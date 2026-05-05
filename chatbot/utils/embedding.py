# src/plugins/chatbot/utils/embedding.py
"""
双阶段语义向量检索 — Embedding 海选 + Reranker 精排

启动时加载 worldbook.json，为每条 content 生成 embedding 并构建 FAISS 索引。
运行时 search() 实现：
  阶段一 (Recall)  — FAISS 内积检索，扩大召回 top_n 条
  阶段二 (Rerank)  — 调用 Reranker API 精排，返回 top_k 条
                     若 Reranker 未开启或调用失败，降级返回 FAISS 结果
"""

import json
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional

import numpy as np
import httpx
from nonebot.log import logger

# FAISS 为可选依赖，缺失时降级为纯关键词匹配
try:
    import faiss
    _HAS_FAISS = True
except ImportError:
    _HAS_FAISS = False
    logger.warning("[SemanticLorebook] faiss 未安装，语义检索不可用，将降级为纯关键词匹配")

# FAISS 召回阶段扩大检索量，供 Reranker 精排
_RECALL_TOP_N = 10


class SemanticLorebook:
    """
    语义世界书检索器（双阶段）

    - 启动时一次性加载 worldbook.json，为每条 content 调用 Embedding API
    - 构建 FAISS Inner Product 索引
    - 运行时 search(query, top_k)：
        1. FAISS 召回 top_n (默认 10) 条
        2. 若 enable_reranker=True，调用 Reranker API 精排后返回 top_k
        3. 否则直接截取 FAISS 结果前 top_k 条
    - 任何异常均降级返回可用结果，不影响主流程
    """

    def __init__(
        self,
        worldbook_path: str,
        api_key: str,
        embedding_url: str,
        embedding_model: str = "BAAI/bge-m3",
        reranker_url: str = "",
        reranker_model: str = "",
        enable_reranker: bool = False,
    ):
        self.api_key = api_key
        self.embedding_url = embedding_url
        self.embedding_model = embedding_model
        self.reranker_url = reranker_url
        self.reranker_model = reranker_model
        self.enable_reranker = enable_reranker

        self.index: Optional[Any] = None
        self.entries: List[Dict[str, Any]] = []
        self.embeddings: Optional[np.ndarray] = None
        self._ready = False

        self._load_and_build(worldbook_path)

    # ─────────────── 启动时构建索引 ───────────────

    def _load_and_build(self, path: str) -> None:
        """加载 worldbook.json 并构建 FAISS 索引（同步，启动时调用一次）。"""
        try:
            wb_path = Path(path)
            if not wb_path.exists():
                logger.warning(f"[SemanticLorebook] worldbook.json 不存在: {path}")
                return

            with open(wb_path, "r", encoding="utf-8") as f:
                raw = json.load(f)

            if not isinstance(raw, list) or len(raw) == 0:
                logger.warning("[SemanticLorebook] worldbook.json 为空或格式错误")
                return

            # 过滤有 content 的条目
            self.entries = [e for e in raw if e.get("content", "").strip()]
            if not self.entries:
                logger.warning("[SemanticLorebook] 无有效条目")
                return

            logger.info(f"[SemanticLorebook] 加载 {len(self.entries)} 条世界书条目，正在生成向量...")

            # 批量获取 embedding
            contents = [e["content"] for e in self.entries]
            vectors = self._batch_embed_sync(contents)

            if vectors is None or len(vectors) == 0:
                logger.warning("[SemanticLorebook] Embedding 生成失败，降级为纯关键词")
                return

            self.embeddings = np.array(vectors, dtype=np.float32)

            # L2 归一化 → Inner Product 等价于 cosine similarity
            faiss.normalize_L2(self.embeddings)

            dim = self.embeddings.shape[1]
            self.index = faiss.IndexFlatIP(dim)
            self.index.add(self.embeddings)

            self._ready = True
            logger.info(
                f"[SemanticLorebook] FAISS 索引构建完成: "
                f"{len(self.entries)} 条, dim={dim}"
            )

        except Exception as e:
            logger.error(f"[SemanticLorebook] 初始化失败: {e}", exc_info=True)
            self._ready = False

    # ─────────────── 同步批量 Embedding ───────────────

    def _batch_embed_sync(self, texts: List[str]) -> Optional[List[List[float]]]:
        """同步批量调用 Embedding API（启动时使用）。"""
        if not self.api_key:
            logger.warning("[SemanticLorebook] 未配置 api_key，跳过 embedding")
            return None

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    self.embedding_url,
                    json={
                        "model": self.embedding_model,
                        "input": texts,
                    },
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                )
                if resp.status_code != 200:
                    logger.error(
                        f"[SemanticLorebook] Embedding API 错误 {resp.status_code}: {resp.text[:200]}"
                    )
                    if resp.status_code in (402, 403):
                        logger.error(
                            f"[SemanticLorebook] ⚠️ API 拒绝访问 ({resp.status_code})，语义检索不可用"
                        )
                    return None

                data = resp.json()
                items = data.get("data", [])
                # 按 index 排序，确保顺序与输入一致
                items.sort(key=lambda x: x.get("index", 0))
                vectors = [item["embedding"] for item in items]
                return vectors

        except Exception as e:
            logger.error(f"[SemanticLorebook] Embedding API 调用失败: {e}")
            return None

    # ─────────────── 运行时异步查询（双阶段） ───────────────

    async def search(self, query: str, top_k: int = 3, threshold: float = 0.3) -> List[Dict[str, Any]]:
        """
        双阶段语义检索：
          阶段一 — FAISS 召回 _RECALL_TOP_N 条
          阶段二 — Reranker 精排（若开启），否则截取 top_k

        返回条目副本（含 content, key, similarity 等）。
        """
        if not self._ready or self.index is None:
            return []

        if not query or not query.strip():
            return []

        try:
            # ── 阶段一：FAISS Recall ──
            query_vec = await self._get_embedding(query)
            if query_vec is None:
                return []

            query_vec = query_vec.reshape(1, -1).astype(np.float32)
            faiss.normalize_L2(query_vec)

            recall_k = min(_RECALL_TOP_N, self.index.ntotal)
            scores, indices = self.index.search(query_vec, recall_k)

            candidates: List[Dict[str, Any]] = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or idx >= len(self.entries):
                    continue
                if score < threshold:
                    continue

                entry = self.entries[idx]
                candidates.append({
                    "key": entry.get("key", []),
                    "content": entry.get("content", ""),
                    "position": entry.get("position", 0),
                    "depth": entry.get("depth", 4),
                    "uid": entry.get("uid", ""),
                    "similarity": round(float(score), 4),
                    "_faiss_idx": int(idx),
                })

            if not candidates:
                return []

            # ── 阶段二：Rerank（可选） ──
            if self.enable_reranker and self.reranker_model and self.reranker_url:
                reranked = await self._rerank(query, candidates, top_k)
                if reranked is not None:
                    return reranked
                # rerank 失败时降级到 FAISS 结果

            # 未开启 reranker 或降级：截取 top_k
            return candidates[:top_k]

        except Exception as e:
            logger.error(f"[SemanticLorebook] search 异常: {e}")
            return []

    # ─────────────── 异步 Rerank ───────────────

    async def _rerank(
        self, query: str, candidates: List[Dict[str, Any]], top_k: int
    ) -> Optional[List[Dict[str, Any]]]:
        """
        调用 Reranker API 对候选结果精排。

        成功返回按相关性重排后的 top_k 条目；失败返回 None 触发降级。
        """
        documents = [c["content"] for c in candidates]

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    self.reranker_url,
                    json={
                        "model": self.reranker_model,
                        "query": query,
                        "documents": documents,
                        "return_documents": False,
                        "top_n": top_k,
                    },
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                )

                if resp.status_code != 200:
                    logger.warning(
                        f"[SemanticLorebook] Reranker API 错误 {resp.status_code}: {resp.text[:200]}"
                    )
                    return None

                data = resp.json()
                results = data.get("results", [])
                if not results:
                    logger.warning("[SemanticLorebook] Reranker 返回空结果")
                    return None

                # 按 reranker 返回的 index 重排本地候选
                reranked: List[Dict[str, Any]] = []
                for item in results:
                    orig_idx = item.get("index")
                    if orig_idx is None or orig_idx < 0 or orig_idx >= len(candidates):
                        continue
                    entry = dict(candidates[orig_idx])  # 浅拷贝
                    entry["rerank_score"] = round(float(item.get("relevance_score", 0)), 4)
                    # 移除内部索引字段
                    entry.pop("_faiss_idx", None)
                    reranked.append(entry)

                return reranked if reranked else None

        except Exception as e:
            logger.warning(f"[SemanticLorebook] Reranker 调用失败，降级为 FAISS 结果: {e}")
            return None

    # ─────────────── 异步单条 Embedding ───────────────

    async def _get_embedding(self, text: str) -> Optional[np.ndarray]:
        """异步调用 Embedding API，返回 (dim,) 向量。"""
        if not self.api_key:
            return None

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    self.embedding_url,
                    json={
                        "model": self.embedding_model,
                        "input": [text],
                    },
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"[SemanticLorebook] Query embedding 失败 {resp.status_code}"
                    )
                    if resp.status_code in (402, 403):
                        from .alert_manager import send_emergency_alert
                        asyncio.create_task(send_emergency_alert(
                            f"⚠️ API 拒绝访问 ({resp.status_code})，语义向量检索不可用，请检查 API 余额或风控状态。"
                        ))
                    return None

                data = resp.json()
                items = data.get("data", [])
                if not items:
                    return None

                return np.array(items[0]["embedding"], dtype=np.float32)

        except Exception as e:
            logger.warning(f"[SemanticLorebook] Query embedding 异常: {e}")
            return None


# ─────────────── 工厂函数 ───────────────

def create_semantic_lorebook(config) -> Optional[SemanticLorebook]:
    """
    工厂函数：从 plugin_config 创建 SemanticLorebook 实例。

    - 无 api_key 时返回 None（降级）
    - 无 faiss 时返回 None（降级）
    - worldbook.json 不存在时返回 None（降级）
    """
    if not _HAS_FAISS:
        return None

    api_key = config.siliconflow_api_key
    if not api_key:
        logger.info("[SemanticLorebook] 未配置 siliconflow_api_key，语义检索不可用")
        return None

    base_url = config.siliconflow_api_url.rstrip("/")
    embedding_url = f"{base_url}/embeddings"
    rerank_url = f"{base_url}/rerank"

    worldbook_path = Path(__file__).resolve().parent.parent.parent.parent.parent / "config" / "worldbook.json"
    if not worldbook_path.exists():
        logger.info(f"[SemanticLorebook] worldbook.json 不存在: {worldbook_path}")
        return None

    return SemanticLorebook(
        worldbook_path=str(worldbook_path),
        api_key=api_key,
        embedding_url=embedding_url,
        embedding_model=config.embedding_model_name,
        reranker_url=rerank_url,
        reranker_model=config.reranker_model_name,
        enable_reranker=config.enable_reranker,
    )
