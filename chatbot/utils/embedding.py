# src/plugins/chatbot/utils/embedding.py
"""
语义向量检索通道 — 封装 DeepSeek Embedding API 与 FAISS 索引

启动时加载 worldbook.json，为每条 content 生成 embedding 并构建 FAISS 索引。
运行时通过 search() 方法返回语义最相近的世界书条目。
"""

import json
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


class SemanticLorebook:
    """
    语义世界书检索器

    - 启动时一次性加载 worldbook.json，为每条 content 调用 embedding API
    - 构建 FAISS Inner Product 索引
    - 运行时 search(query, top_k) 返回语义最相近的条目
    - 任何异常均降级返回空列表，不影响主流程
    """

    def __init__(
        self,
        worldbook_path: str,
        api_key: str,
        api_url: str = "https://api.deepseek.com/v1/embeddings",
        model: str = "deepseek-embedding",
    ):
        self.api_key = api_key
        self.api_url = api_url
        self.model = model
        self.index: Optional[Any] = None
        self.entries: List[Dict[str, Any]] = []  # 原始条目（含 content, key 等）
        self.embeddings: Optional[np.ndarray] = None  # (n, dim)
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
        """
        同步批量调用 embedding API（启动时使用）。
        DeepSeek embedding API 支持批量输入。
        """
        if not self.api_key:
            logger.warning("[SemanticLorebook] 未配置 deepseek_api_key，跳过 embedding")
            return None

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    self.api_url,
                    json={
                        "model": self.model,
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

    # ─────────────── 运行时异步查询 ───────────────

    async def search(self, query: str, top_k: int = 3, threshold: float = 0.3) -> List[Dict[str, Any]]:
        """
        语义检索：返回与 query 最相似的 top_k 个条目。

        - threshold: 最低相似度阈值（cosine similarity），低于此值不返回
        - 返回条目副本（含 content, key, similarity 等），供 Node 端直接激活
        - 任何异常或索引不可用时返回空列表（降级为纯关键词）
        """
        if not self._ready or self.index is None:
            return []

        if not query or not query.strip():
            return []

        try:
            query_vec = await self._get_embedding(query)
            if query_vec is None:
                return []

            query_vec = query_vec.reshape(1, -1).astype(np.float32)
            faiss.normalize_L2(query_vec)

            k = min(top_k, self.index.ntotal)
            scores, indices = self.index.search(query_vec, k)

            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or idx >= len(self.entries):
                    continue
                if score < threshold:
                    continue

                entry = self.entries[idx]
                results.append({
                    "key": entry.get("key", []),
                    "content": entry.get("content", ""),
                    "position": entry.get("position", 0),
                    "depth": entry.get("depth", 4),
                    "uid": entry.get("uid", ""),
                    "similarity": round(float(score), 4),
                })

            return results

        except Exception as e:
            logger.error(f"[SemanticLorebook] search 异常: {e}")
            return []

    async def _get_embedding(self, text: str) -> Optional[np.ndarray]:
        """异步调用 embedding API，返回 (dim,) 向量。"""
        if not self.api_key:
            return None

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    self.api_url,
                    json={
                        "model": self.model,
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

    api_key = config.deepseek_api_key
    if not api_key:
        logger.info("[SemanticLorebook] 未配置 deepseek_api_key，语义检索不可用")
        return None

    # embedding API URL：从 deepseek_api_url 推导
    # deepseek_api_url = "https://api.deepseek.com/chat/completions"
    # embedding_url     = "https://api.deepseek.com/v1/embeddings"
    api_url = config.deepseek_api_url
    base = api_url.rsplit("/", 1)[0] if "/" in api_url else api_url
    # 如果 base 以 /chat/completions 结尾，取前面部分
    if base.endswith("/chat"):
        base = base[: -len("/chat")]
    embedding_url = f"{base}/v1/embeddings"

    worldbook_path = Path(__file__).parent.parent / "data" / "worldbook.json"
    if not worldbook_path.exists():
        # 尝试 engine 同级的 data 目录
        alt_path = Path(__file__).parent.parent / "engine" / "data" / "worldbook.json"
        if alt_path.exists():
            worldbook_path = alt_path
        else:
            logger.info(f"[SemanticLorebook] worldbook.json 不存在: {worldbook_path}")
            return None

    return SemanticLorebook(
        worldbook_path=str(worldbook_path),
        api_key=api_key,
        api_url=embedding_url,
    )
