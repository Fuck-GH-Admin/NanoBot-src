"""
AESAM Adapter — 事件溯源运行时的外围桥接层

将 ConversationRuntime (新心脏) 与旧版 AgentService 基础设施 (repo, http_client,
prompt_adapter, registry, config) 对接，实现无痛灰度接入。

用法:
    adapter = AesamAdapter(repo, http_client, prompt_adapter, registry, config)
    result = await adapter.handle_turn(session_id, user_text, context)
"""

import re
import uuid
import asyncio
import logging
from typing import Dict, Any, List

import httpx

from .events import ConversationEvent, EventType
from .store_protocol import EventStoreProtocol
from .engine import ConversationRuntime
from .projections import StateProjector

logger = logging.getLogger("aesam_adapter")


# ---------------------------------------------------------------------------
# EventStoreAdapter: EventStoreProtocol 的 SQLite 实现
# ---------------------------------------------------------------------------

class EventStoreAdapter:
    """桥接 MemoryRepository 的连接池，实现 EventStoreProtocol。"""

    def __init__(self, repo):
        self._repo = repo

    async def append_event(self, event: ConversationEvent) -> ConversationEvent:
        """将事件持久化到 event_log 表。失败时抛出异常，不静默降级。"""
        from ..repositories.models import EventLog

        async with self._repo._get_session() as session:
            session.add(EventLog(
                event_id=event.event_id,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                session_id=event.session_id,
                epoch=event.epoch,
                type=event.type.value if isinstance(event.type, EventType) else event.type,
                source=event.source,
                payload=dict(event.payload),
            ))
            await session.commit()
        return event

    async def load_stream(self, session_id: str) -> List[ConversationEvent]:
        """从 event_log 表加载事件流。失败时抛出异常，不静默降级。"""
        from ..repositories.models import EventLog
        from sqlalchemy import select
        from types import MappingProxyType

        async with self._repo._get_session() as session:
            stmt = (
                select(EventLog)
                .where(EventLog.session_id == session_id)
                .order_by(EventLog.id.asc())
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        return [
            ConversationEvent(
                event_id=r.event_id,
                correlation_id=r.correlation_id,
                causation_id=r.causation_id,
                session_id=r.session_id,
                epoch=r.epoch,
                type=EventType(r.type),
                source=r.source,
                payload=MappingProxyType(r.payload or {}),
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# AesamAdapter: 将 ConversationRuntime 接入旧版基础设施
# ---------------------------------------------------------------------------

_INVOKE_RE = re.compile(
    r"""<invoke\s+name=["']([^"']+)["']\s*>(.*?)</invoke>""",
    re.DOTALL | re.IGNORECASE,
)

_PARAM_RE = re.compile(
    r"""<parameter\s+name=["']([^"']+)["']\s*>(.*?)</parameter>""",
    re.DOTALL | re.IGNORECASE,
)


class AesamAdapter:
    """
    事件溯源运行时适配器。

    封装 ConversationRuntime.process_turn 所需的 logic_runner / actor_runner
    闭包，对外暴露 handle_turn 方法，返回与旧版 run_agent 兼容的字典格式。
    """

    def __init__(
        self,
        repo,
        http_client: httpx.AsyncClient,
        prompt_adapter,
        registry,
        config,
    ):
        self.repo = repo
        self.http_client = http_client
        self.prompt_adapter = prompt_adapter
        self.registry = registry
        self.config = config

        store = EventStoreAdapter(repo)
        self.runtime = ConversationRuntime(store)

    async def handle_turn(
        self,
        session_id: str,
        user_text: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        处理一轮对话：逻辑脑调度 → 工具执行 → 演员脑渲染。

        返回格式与旧版 run_agent 兼容: {"text": str, "images": list}
        """

        # -- 从 context 提取共享数据 --
        perm_srv = context.get("permission_service")
        user_id = context.get("user_id", "")
        is_admin = context.get("is_admin", False)
        tools = self.registry.get_all_schemas(perm_srv, user_id, is_admin)

        if context.get("is_tome") or context.get("is_reply_bot") or context.get("has_wake_word"):
            tools = [t for t in tools if t["function"]["name"] != "exit_session"]

        group_id = context.get("group_id", 0)
        wb_content = context.get("_worldbook_entries", "")

        # -- 读取近期消息 --
        try:
            recent_msgs = await asyncio.wait_for(
                self.repo.get_recent_messages(session_id, limit=30),
                timeout=3.0,
            )
        except asyncio.TimeoutError:
            recent_msgs = []

        messages = []
        for raw in recent_msgs:
            if raw["role"] == "tool":
                continue
            if raw["role"] == "assistant" and raw.get("tool_calls"):
                continue
            msg: Dict[str, Any] = {
                "role": raw["role"],
                "content": raw.get("content", ""),
            }
            if raw.get("name"):
                msg["name"] = raw["name"]
            if raw.get("user_id"):
                msg["user_id"] = raw["user_id"]
            messages.append(msg)

        snapshot: Dict[str, Any] = {}
        logic_model = getattr(self.config, "logic_model_name", "") or self.config.deepseek_model_name

        # ---- logic_runner closure ----
        async def logic_runner(view, text: str) -> Dict[str, Any]:
            """逻辑脑: 编译 prompt -> LLM -> XML 解析 -> 返回工具调用结果"""
            lorebook_context = {
                "group_id": group_id,
                "active_uids": list({m.get("user_id") for m in messages if m.get("user_id")}),
                "token_arbitration_enabled": getattr(self.config, "token_arbitration_enabled", False),
                "_matched_rule": context.get("_matched_rule"),
                "is_tome": context.get("is_tome"),
                "is_reply_bot": context.get("is_reply_bot"),
                "has_wake_word": context.get("has_wake_word"),
            }

            logic_msgs = self.prompt_adapter.compile_logic_prompt(
                chat_history=messages,
                snapshot=snapshot,
                context=lorebook_context,
                tools=tools,
                worldbook_entries=wb_content,
            )

            # 调用 LLM
            msg = await self._call_llm(logic_msgs, model=logic_model, temperature=0.0)
            if msg is None:
                return {}

            assistant_content = msg.get("content", "")

            # XML 解析: 提取 <invoke> 标签
            match = _INVOKE_RE.search(assistant_content)
            if not match:
                return {}

            func_name = match.group(1).strip()
            params_block = match.group(2)
            arguments: Dict[str, Any] = {}

            if params_block.strip():
                for p_match in _PARAM_RE.finditer(params_block):
                    val = p_match.group(2).strip()
                    if val.lower() == "true":
                        val = True
                    elif val.lower() == "false":
                        val = False
                    elif val.isdigit():
                        val = int(val)
                    arguments[p_match.group(1).strip()] = val

            # Execute tool via registry
            result_text = ""
            images = []
            try:
                result_text, images = await asyncio.wait_for(
                    self.registry.execute_tool(func_name, arguments, context),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                result_text = "Tool execution timed out."
            except Exception as e:
                result_text = f"Tool error: {e}"

            tool_results: Dict[str, Any] = {}
            tool_results[func_name] = {
                "result": result_text[:300],
                "images": images,
            }
            return tool_results

        # ---- actor_runner closure ----
        async def actor_runner(view, text: str, tool_results: dict) -> str:
            """演员脑: 注入世界书 + 工具结果通知，返回纯文本回复。"""

            tool_logs = []
            for name, res in tool_results.items():
                r_text = res.get("result", "") if isinstance(res, dict) else str(res)
                tool_logs.append(f"[{name}] {r_text[:300]}")

            system_notification = ""
            if tool_logs:
                system_notification = "[SYSTEM_TOOL_RESULT] " + "\n".join(tool_logs)

            lorebook_context = {
                "group_id": group_id,
                "active_uids": list({m.get("user_id") for m in messages if m.get("user_id")}),
                "token_arbitration_enabled": getattr(self.config, "token_arbitration_enabled", False),
                "_matched_rule": context.get("_matched_rule"),
            }

            actor_msgs = self.prompt_adapter.compile_actor_prompt(
                chat_history=messages,
                snapshot=snapshot,
                context=lorebook_context,
                system_notification=system_notification,
                worldbook_entries=wb_content,
            )

            final_msg = await self._call_llm(actor_msgs, temperature=0.7)
            if final_msg is None:
                return "大脑短路了，等一下再试吧..."
            return final_msg.get("content", "")

        # ---- orchestrate via ConversationRuntime ----
        final_reply = await self.runtime.process_turn(
            session_id, user_text, logic_runner, actor_runner
        )

        # Persist assistant reply
        from datetime import datetime
        await self.repo.add_message(
            session_id=session_id,
            role="assistant",
            content=final_reply,
            timestamp=datetime.now().isoformat(),
            message_fingerprint=uuid.uuid4().hex,
        )

        return {"text": final_reply, "images": []}

    # ---- LLM caller (mirrors AgentService._call_llm) ----

    async def _call_llm(
        self,
        messages: list,
        model: str = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> dict | None:
        """Unified LLM caller, mirrors AgentService._call_llm."""
        model = model or self.config.deepseek_model_name
        is_reasoner = "reasoner" in model.lower() or "r1" in model.lower()

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not is_reasoner:
            payload["thinking"] = {"type": "disabled"}

        try:
            resp = await self.http_client.post(
                self.config.deepseek_api_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.config.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=getattr(self.config, "agent_request_timeout", 60.0),
            )
            if resp.status_code != 200:
                logger.error(f"[AesamAdapter] LLM API error {resp.status_code}")
                return None
            data = resp.json()
        except Exception as e:
            logger.error(f"[AesamAdapter] LLM request failed: {e}")
            return None

        choices = data.get("choices") or []
        if not choices:
            return None
        return choices[0].get("message") or {}