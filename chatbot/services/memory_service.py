# src/plugins/chatbot/services/memory_service.py

import asyncio
import json
import uuid
from typing import Dict, List, Any

import httpx
from sqlalchemy import select
from nonebot.log import logger

from ..config import plugin_config
from ..repositories.memory_repo import MemoryRepository
from ..repositories.models import CompactionJournal
from ..utils.alert_manager import send_emergency_alert


# ─────────────────── 常量 ───────────────────
SUMMARY_THRESHOLD = 15
MAX_RETRIES = 3
BACKOFF_BASE = 2          # 指数退避基数（秒）
CONCURRENCY_LIMIT = 2     # 同时执行 _do_process 的最大协程数
STALE_SECONDS = 300       # 僵尸任务判定阈值（秒）

# Function Calling 工具定义：强制 LLM 结构化输出
MEMORY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_memory_graph",
        "description": (
            "更新群组的记忆图谱。必须调用此工具来输出结构化的记忆更新。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group_summary": {
                    "type": "string",
                    "description": "对群组整体事件的客观摘要，提取关键信息、事件发展和重要结论。",
                },
                "user_traits": {
                    "type": "array",
                    "description": "从对话中提取到的群友特征列表。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "user_id": {
                                "type": "string",
                                "description": "用户的唯一数字 ID（QQ 号）",
                            },
                            "content": {
                                "type": "string",
                                "description": "提取到的特征内容，如喜好、性格、关系等",
                            },
                            "confidence": {
                                "type": "number",
                                "description": "置信度 0-1，根据对话内容的明确程度判断",
                            },
                        },
                        "required": ["user_id", "content", "confidence"],
                    },
                },
                "entities": {
                    "type": "array",
                    "description": "对话中提及的新实体或需要更新的实体",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "实体的唯一标识（可用QQ号加前缀，如 user_12345）",
                            },
                            "name": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": ["person", "object", "location", "event", "concept"],
                            },
                            "attributes": {
                                "type": "object",
                                "description": "实体附加属性",
                            },
                        },
                        "required": ["entity_id", "name", "type"],
                    },
                },
                "relations": {
                    "type": "array",
                    "description": "实体之间的关系或关系变更",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject_entity": {
                                "type": "string",
                                "description": "主体实体ID",
                            },
                            "predicate": {
                                "type": "string",
                                "description": "谓语，如 likes, trusts, member_of",
                            },
                            "object_entity": {
                                "type": "string",
                                "description": "客体实体ID",
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "evidence_msg_ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "佐证消息的ID",
                            },
                        },
                        "required": [
                            "subject_entity",
                            "predicate",
                            "object_entity",
                            "confidence",
                        ],
                    },
                },
            },
            "required": ["group_summary", "user_traits"],
        },
    },
}


class MemoryService:
    """
    后台记忆特工（高可用版）

    - asyncio.Queue + 单消费者协程（_worker），有序出队
    - Semaphore 限流，防止并发 LLM 调用过多
    - 指数退避重试（最多 MAX_RETRIES 次），超限进入死信队列（_dlq）
    - CompactionJournal 表持久化任务状态，支持僵尸任务恢复
    - graceful shutdown：drain 队列 → 等待 worker 结束 → 关闭 httpx
    """

    def __init__(self):
        self.repo = MemoryRepository()
        self.http_client = httpx.AsyncClient(timeout=90.0)

        # ── 队列 & 控制 ──
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._running = False
        self._worker_task: asyncio.Task | None = None
        self._concurrency_limiter = asyncio.Semaphore(CONCURRENCY_LIMIT)

        # ── 死信队列（内存，仅供运维观测） ──
        self._dlq: List[Dict[str, Any]] = []

        # ── per-session 互斥（防止同一 session 并发压缩） ──
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    # ─────────────── 生命周期 ───────────────

    async def start_consumer(self) -> None:
        """启动消费者协程（在 on_startup 中调用）。"""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("[MemoryService] 消费者协程已启动")

        # 恢复僵尸任务
        await self._recover_stale_journals()

    async def shutdown(self) -> None:
        """优雅关闭：停止接受新任务 → drain 队列 → 等待 worker → 关闭 httpx。"""
        logger.info("[MemoryService] 正在优雅关闭...")
        self._running = False

        # 等待队列清空（最多 30 秒）
        try:
            await asyncio.wait_for(self._queue.join(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("[MemoryService] 队列 drain 超时，强制退出")

        # 取消 worker
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        await self.http_client.aclose()
        logger.info("[MemoryService] 已关闭")

    async def close(self):
        """兼容旧接口，委托给 shutdown。"""
        await self.shutdown()

    # ─────────────── 僵尸任务恢复 ───────────────

    async def _recover_stale_journals(self) -> None:
        """启动时扫描 CompactionJournal 中的僵尸 running 任务，重新入队或标记 dead。"""
        try:
            stale = await self.repo.get_stale_journals(STALE_SECONDS)
            requeue_ids = []
            dead_ids = []
            for j in stale:
                if j["retry_count"] < j["max_retries"]:
                    requeue_ids.append(j["journal_id"])
                else:
                    dead_ids.append(j["journal_id"])

            if dead_ids:
                await self.repo.mark_journals_failed(dead_ids)
                for jid in dead_ids:
                    self._dlq.append({"journal_id": jid, "reason": "stale_exhausted"})
                logger.warning(f"[MemoryService] {len(dead_ids)} 个僵尸任务标记为 dead")

            for jid in requeue_ids:
                # 从 journal_id 反查 session_id（通过 stale 列表）
                session_id = next(
                    (j["session_id"] for j in stale if j["journal_id"] == jid), None
                )
                if session_id:
                    await self._queue.put(session_id)
                    await self.repo.update_compaction_journal(jid, status="pending")
                    logger.info(f"[MemoryService] 僵尸任务 {jid} 已重新入队")

        except Exception as e:
            logger.error(f"[MemoryService] 恢复僵尸任务失败: {e}", exc_info=True)

    # ─────────────── 对外接口 ───────────────

    async def process_session_memory(self, session_id: str) -> None:
        """
        入口：根据 enable_task_queue 决定走队列还是直接执行。

        - enable_task_queue=True:  入队（fire-and-forget + 持久化 + 重试）
        - enable_task_queue=False: 直接 asyncio.create_task 调用 _do_process（轻量模式）
        """
        if plugin_config.enable_task_queue:
            return await self._enqueue(session_id)
        else:
            # 轻量模式：fire-and-forget，无持久化、无重试
            asyncio.create_task(self._direct_process(session_id))

    async def _direct_process(self, session_id: str) -> None:
        """轻量模式：直接执行，异常仅记录日志。"""
        try:
            lock = await self._get_lock(session_id)
            async with lock:
                await self._do_process(session_id)
        except Exception as e:
            logger.error(f"[MemoryService] 直接处理 {session_id} 失败: {e}", exc_info=True)

    async def _enqueue(self, session_id: str) -> None:
        """高可用模式：入队 + Journal 持久化。"""
        if not self._running:
            logger.debug("[MemoryService] 消费者未启动，跳过入队")
            return

        # 去重：检查队列中是否已有相同 session_id
        if session_id in self._queue._queue:  # type: ignore[attr-defined]
            logger.debug(f"[MemoryService] {session_id} 已在队列中，跳过重复入队")
            return

        # 创建 Journal 记录
        journal_id = uuid.uuid4().hex
        try:
            await self.repo.insert_compaction_journal(journal_id, session_id, MAX_RETRIES)
        except Exception as e:
            logger.error(f"[MemoryService] 创建 Journal 失败: {e}")
            return

        await self._queue.put(session_id)
        logger.debug(f"[MemoryService] {session_id} 已入队 (journal={journal_id})")

    # ─────────────── 消费者协程 ───────────────

    async def _worker(self) -> None:
        """单消费者协程，从队列中逐个取出 session_id 并处理。"""
        while self._running:
            try:
                session_id = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._do_process_with_retry(session_id)
            except Exception as e:
                logger.error(
                    f"[MemoryService] worker 处理 {session_id} 未捕获异常: {e}",
                    exc_info=True,
                )
            finally:
                self._queue.task_done()

    # ─────────────── 带重试的核心逻辑 ───────────────

    async def _do_process_with_retry(self, session_id: str) -> None:
        """带指数退避重试的处理逻辑。失败超过 MAX_RETRIES 次进入死信队列。"""
        # 查找对应的 journal_id（最近一条 pending/running 的记录）
        journal_id = await self._find_active_journal(session_id)
        if not journal_id:
            logger.warning(f"[MemoryService] {session_id} 无活跃 Journal，跳过")
            return

        await self.repo.update_compaction_journal(journal_id, status="running")

        last_error = ""
        for attempt in range(MAX_RETRIES):
            try:
                async with self._concurrency_limiter:
                    lock = await self._get_lock(session_id)
                    async with lock:
                        await self._do_process(session_id)

                # 成功
                await self.repo.update_compaction_journal(
                    journal_id, status="success", retry_count=attempt
                )
                logger.info(f"[MemoryService] {session_id} 记忆压缩成功 (attempt={attempt + 1})")
                return

            except Exception as e:
                last_error = str(e)

                # API 拒绝服务（402/403）：不消耗重试次数，直接退出，避免雪崩
                if "402" in last_error or "403" in last_error:
                    logger.error(
                        f"[MemoryService] {session_id} API 拒绝访问，跳过重试，等待恢复"
                    )
                    await self.repo.update_compaction_journal(
                        journal_id, status="pending", last_error=last_error
                    )
                    return

                logger.warning(
                    f"[MemoryService] {session_id} 第 {attempt + 1}/{MAX_RETRIES} 次失败: {last_error}"
                )
                await self.repo.update_compaction_journal(
                    journal_id, retry_count=attempt + 1, last_error=last_error
                )

                if attempt < MAX_RETRIES - 1:
                    backoff = BACKOFF_BASE ** attempt
                    logger.info(f"[MemoryService] {backoff}s 后重试...")
                    await asyncio.sleep(backoff)

        # 全部重试用尽 → 死信
        await self.repo.update_compaction_journal(journal_id, status="dead", last_error=last_error)
        self._dlq.append({
            "journal_id": journal_id,
            "session_id": session_id,
            "reason": "max_retries_exhausted",
            "last_error": last_error,
        })
        logger.error(
            f"[MemoryService] {session_id} 记忆压缩失败（已重试 {MAX_RETRIES} 次），进入死信队列"
        )

    async def _find_active_journal(self, session_id: str) -> str | None:
        """查找该 session 最近一条 pending 或 running 状态的 journal_id。"""
        async with self.repo._get_session() as session:
            stmt = (
                select(CompactionJournal.journal_id)
                .where(
                    CompactionJournal.session_id == session_id,
                    CompactionJournal.status.in_(["pending", "running"]),
                )
                .order_by(CompactionJournal.created_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    # ─────────────── per-session 锁 ───────────────

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """按 session_id 获取或创建锁"""
        if session_id not in self._locks:
            async with self._global_lock:
                if session_id not in self._locks:
                    self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    # ─────────────── 核心处理逻辑（在锁 + 信号量内执行） ───────────────

    async def _do_process(self, session_id: str) -> None:
        """核心处理逻辑"""

        # 1. 获取未总结消息
        unsummarized = await self.repo.get_unsummarized_messages(session_id)
        if len(unsummarized) < SUMMARY_THRESHOLD:
            logger.debug(
                f"[MemoryService] {session_id} 未总结消息 {len(unsummarized)} 条 "
                f"< 阈值 {SUMMARY_THRESHOLD}，跳过"
            )
            return

        logger.info(
            f"[MemoryService] {session_id} 触发记忆压缩，"
            f"未总结消息 {len(unsummarized)} 条"
        )

        # 2. 获取已有的群组摘要
        existing_summary = await self.repo.get_group_summary(session_id)

        # 3. 格式化对话文本（注入 user_id 以锚定画像）
        conversation_lines: List[str] = []
        message_ids: List[int] = []
        for msg in unsummarized:
            uid = msg.get("user_id", "")
            name = msg.get("name", "Unknown")
            content = msg.get("content", "").strip()
            if not content:
                continue
            id_prefix = f"[ID: {uid}] " if uid else ""
            conversation_lines.append(f"{id_prefix}{name}: {content}")
            message_ids.append(msg["id"])

        if not conversation_lines:
            return

        conversation_text = "\n".join(conversation_lines)

        # 4. 构建提示词
        system_prompt = (
            "You are a conversation analyst. Analyze the chat history below and "
            "call the `update_memory_graph` tool to record what you learned.\n\n"
            "Rules:\n"
            "- Extract a factual group summary capturing key events and developments.\n"
            "- Extract individual user traits (preferences, personality, relationships).\n"
            "- Use the numeric user_id (in brackets like [ID: 123456]) to identify users.\n"
            "- Assign confidence based on how explicitly the trait was expressed.\n"
            "- Be concise. Only record genuinely new or changed traits.\n\n"
            "Entity & Relation Extraction:\n"
            "- Extract entities mentioned in the conversation: people (use entity_id "
            "like `user_QQ号`), important objects, locations, events, concepts.\n"
            "- Identify relationships between entities as Subject-Predicate-Object "
            "triples (e.g. 'Alice likes cats', 'Bob is group admin'). "
            "Attach a confidence score and evidence message IDs when possible.\n"
            "- If a known relation appears with new evidence, still output it — the "
            "system will merge automatically.\n"
            "- Only extract genuinely new or clearly changed information; avoid "
            "duplicates."
        )

        user_content = ""
        if existing_summary:
            user_content += f"Existing group summary:\n{existing_summary}\n\n"
        user_content += f"Recent conversation to analyze:\n{conversation_text}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # 5. 调用 DeepSeek（带 Function Calling）
        api_key = plugin_config.deepseek_api_key
        api_url = plugin_config.deepseek_api_url
        model = plugin_config.deepseek_memory_model_name

        if not api_key:
            logger.warning("[MemoryService] 未配置 deepseek_api_key，跳过记忆压缩")
            return

        request_body = {
            "model": model,
            "messages": messages,
            "tools": [MEMORY_TOOL_SCHEMA],
            "tool_choice": {"type": "function", "function": {"name": "update_memory_graph"}},
            "stream": False,
            "temperature": 0.3,
            "max_tokens": 8000,
            "thinking": {"type": "disabled"},
        }

        try:
            resp = await self.http_client.post(
                api_url,
                json=request_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            if resp.status_code != 200:
                if resp.status_code in (402, 403):
                    asyncio.create_task(send_emergency_alert(
                        f"⚠️ API 拒绝访问 ({resp.status_code})，记忆压缩功能不可用，请尽快检查 API 余额或风控状态。"
                    ))
                raise RuntimeError(
                    f"DeepSeek API 错误 {resp.status_code}: {resp.text[:300]}"
                )

            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"请求 DeepSeek 失败: {e}") from e

        # 6. 解析 Tool Call 返回
        choices = data.get("choices", [])
        if not choices:
            logger.warning("[MemoryService] DeepSeek 返回空 choices")
            return

        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls", [])

        if not tool_calls:
            logger.warning("[MemoryService] LLM 未调用工具，跳过持久化")
            return

        # 取第一个 tool call 的参数
        tc = tool_calls[0]
        func = tc.get("function", {})
        arguments_str = func.get("arguments", "{}")

        try:
            arguments = (
                json.loads(arguments_str)
                if isinstance(arguments_str, str)
                else arguments_str
            )
        except json.JSONDecodeError:
            logger.error(
                f"[MemoryService] Tool Call 参数解析失败: {arguments_str[:200]}"
            )
            return

        # 7. 持久化：群组摘要
        new_summary = arguments.get("group_summary", "").strip()
        if new_summary:
            if existing_summary:
                merged = f"{existing_summary}\n{new_summary}"
            else:
                merged = new_summary
            await self.repo.upsert_group_summary(session_id, merged)
            logger.info(f"[MemoryService] {session_id} 群组摘要已更新，长度 {len(merged)}")

        # 8. 持久化：群友画像（按 user_id 分组批量写入）
        user_traits = arguments.get("user_traits", [])
        if user_traits:
            traits_by_user: Dict[str, List[Dict[str, Any]]] = {}
            for trait in user_traits:
                uid = str(trait.get("user_id", "")).strip()
                content = str(trait.get("content", "")).strip()
                confidence = float(trait.get("confidence", 0.5))
                if uid and content:
                    traits_by_user.setdefault(uid, []).append(
                        {"content": content, "confidence": confidence}
                    )

            total_written = 0
            for uid, traits_list in traits_by_user.items():
                count = await self.repo.upsert_user_traits(session_id, uid, traits_list)
                total_written += count

            logger.info(
                f"[MemoryService] {session_id} 群友画像已更新，"
                f"涉及 {len(traits_by_user)} 人，共 {total_written} 条特征"
            )

        # 8b. 持久化：实体
        entities_raw = arguments.get("entities", [])
        if entities_raw:
            entities_to_upsert = []
            for ent in entities_raw:
                eid = str(ent.get("entity_id", "")).strip()
                name = str(ent.get("name", "")).strip()
                etype = str(ent.get("type", "")).strip()
                if eid and name and etype:
                    entities_to_upsert.append({
                        "entity_id": eid,
                        "session_id": session_id,
                        "name": name,
                        "type": etype,
                        "attributes": ent.get("attributes", {}),
                    })
            if entities_to_upsert:
                count = await self.repo.upsert_entities(entities_to_upsert)
                logger.info(f"[MemoryService] {session_id} 实体已更新，共 {count} 个")

        # 8c. 持久化：关系
        relations_raw = arguments.get("relations", [])
        if relations_raw:
            relations_to_upsert = []
            for rel in relations_raw:
                subj = str(rel.get("subject_entity", "")).strip()
                pred = str(rel.get("predicate", "")).strip()
                obj = str(rel.get("object_entity", "")).strip()
                conf = float(rel.get("confidence", 0.5))
                evidence = rel.get("evidence_msg_ids", [])
                if subj and pred and obj:
                    relations_to_upsert.append({
                        "session_id": session_id,
                        "subject_entity": subj,
                        "predicate": pred,
                        "object_entity": obj,
                        "confidence": conf,
                        "evidence_msg_ids": evidence if isinstance(evidence, list) else [],
                    })
            if relations_to_upsert:
                count = await self.repo.upsert_relations(relations_to_upsert)
                logger.info(f"[MemoryService] {session_id} 关系已更新，共 {count} 条")

        # 9. 移动游标：标记已总结
        marked = await self.repo.mark_messages_summarized(message_ids)
        logger.info(f"[MemoryService] {session_id} 已标记 {marked} 条消息为已总结")
