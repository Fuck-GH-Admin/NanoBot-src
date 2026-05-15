# src/plugins/chatbot/services/agent_service.py

import asyncio
import json
import re
import time
import uuid
import httpx
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from nonebot.log import logger

from ..config import plugin_config
from ..utils.path_utils import WORLDBOOK_PATH
from .prompt_adapter import PromptAdapter
from .rule_engine import RuleEngine, RuleEngineCore, SQLiteRuleProvider
from ..repositories.memory_repo import MemoryRepository
from ..repositories.rule_repo import RuleRepository
from ..tools.registry import AgentToolRegistry
from ..tools.agent_tools import (
    GenerateImageTool, SearchAcgImageTool,
    RecommendBookTool, JmDownloadTool,
    LearnRuleTool, ForgetRuleTool,
    MarkTaskCompleteTool, ExitSessionTool, NoOpTool,
)
from .memory_service import MemoryService
from .world_book import WorldBook
from ..utils.embedding import create_semantic_lorebook
from ..utils.alert_manager import send_emergency_alert, reset_cooldown
from ..utils.session_dumper import SessionDumper

# 发给 Node.js 的近期消息轮次上限
RECENT_MESSAGES_LIMIT = 30

# 重复检测相似度阈值
JACCARD_THRESHOLD = 0.9

# 数据层调用超时（秒）
DB_QUERY_TIMEOUT = 3.0

# 工具执行超时（秒）
TOOL_EXEC_TIMEOUT = 15.0

# 语义检索超时（秒）
SEMANTIC_SEARCH_TIMEOUT = 3.0

# 逻辑脑历史截断上限
LOGIC_HISTORY_LIMIT = 15

# 终态工具集合：调用后必须立即终止逻辑循环，流转至演员脑
TERMINAL_TOOLS = {"no_op", "mark_task_complete", "exit_session"}

# 静默工具集合：执行结果不注入演员脑（避免噪音污染人格渲染）
SILENT_TOOLS = {"no_op", "mark_task_complete"}

# 工具执行结果截断长度
TOOL_RESULT_MAX_LEN = 300


def _jaccard(a: str, b: str) -> float:
    """计算两个字符串的 Jaccard 相似度（基于词集合）。"""
    set_a = set(a.split())
    set_b = set(b.split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


class AgentService:
    def __init__(self):
        self.repo = MemoryRepository()
        self.memory_service = MemoryService()
        self.prompt_adapter = PromptAdapter()
        self.registry = AgentToolRegistry()
        self.http_client = httpx.AsyncClient()
        self.semantic_lorebook = create_semantic_lorebook(plugin_config)
        self.world_book = WorldBook(
            str(WORLDBOOK_PATH)
        )
        self.rule_engine = RuleEngine(SQLiteRuleProvider())
        self.rule_repo = RuleRepository()
        self._register_tools()

    def _register_tools(self):
        self.registry.register(GenerateImageTool())
        self.registry.register(SearchAcgImageTool())
        self.registry.register(RecommendBookTool())
        self.registry.register(JmDownloadTool())
        self.registry.register(LearnRuleTool())
        self.registry.register(ForgetRuleTool())
        if plugin_config.enable_dynamic_loop:
            self.registry.register(MarkTaskCompleteTool())
        self.registry.register(ExitSessionTool())
        self.registry.register(NoOpTool())

    async def close(self):
        await self.http_client.aclose()

    async def _call_llm(
        self,
        messages: list,
        model: str = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Optional[Dict[str, Any]]:
        """
        统一 LLM 调用入口。
        - R1/reasoner 模型自动跳过 thinking 参数。
        - 402/403 触发紧急告警。
        - 返回 choices[0].message 字典，失败返回 None。
        - 工具 schema 已通过 XML 注入 system prompt，不再使用原生 function calling。
        """
        model = model or plugin_config.deepseek_model_name

        # R1/reasoner 模型不支持 thinking 参数
        is_reasoner = "reasoner" in model.lower() or "r1" in model.lower()

        api_payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not is_reasoner:
            api_payload["thinking"] = {"type": "disabled"}

        # 脱敏：替换 API Key
        _safe_payload = json.dumps(api_payload, ensure_ascii=False, default=str)
        _safe_key = plugin_config.deepseek_api_key
        if _safe_key:
            _safe_payload = _safe_payload.replace(_safe_key, "***REDACTED***")
        logger.info(f"[Agent] LLM 请求体: {_safe_payload}")

        try:
            resp = await self.http_client.post(
                plugin_config.deepseek_api_url,
                json=api_payload,
                headers={
                    "Authorization": f"Bearer {plugin_config.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=plugin_config.agent_request_timeout,
            )
            if resp.status_code != 200:
                logger.error(f"[Agent] LLM API error {resp.status_code}: {resp.text}")
                if resp.status_code in (402, 403):
                    asyncio.create_task(send_emergency_alert(
                        f"⚠️ API 拒绝访问 ({resp.status_code})，聊天功能不可用，请尽快检查 API 余额或风控状态。"
                    ))
                return None
            data = resp.json()
            logger.info(f"[Agent] LLM 响应体: {json.dumps(data, ensure_ascii=False, default=str)}")
            reset_cooldown()
        except Exception as e:
            logger.error(f"[Agent] 请求 LLM 失败: {e}")
            return None

        choices = data.get("choices") or []
        if not choices:
            return None
        return choices[0].get("message") or {}

    @staticmethod
    def _build_session_id(group_id: int, user_id: str) -> str:
        if group_id and int(group_id) != 0:
            return f"group_{group_id}"
        return f"private_{user_id}"

    @staticmethod
    def _to_openai_message(msg: Dict[str, Any]) -> Dict[str, Any] | None:
        """
        将数据库读出的消息字典转换为 OpenAI 兼容格式。
        透传 name、user_id 保证群聊多角色区分。
        返回 None 表示该消息应被跳过（遗留工具消息）。
        """
        # 防御：跳过遗留的工具相关消息
        if msg["role"] == "tool" or (msg["role"] == "assistant" and msg.get("tool_calls")):
            logger.warning(
                f"[Agent] 发现未迁移的遗留工具消息，已跳过: "
                f"role={msg['role']}, id={msg.get('id')}, "
                f"content={msg.get('content','')[:80]}"
            )
            return None

        result = {
            "role": msg["role"],
            "content": msg.get("content", ""),
        }
        if "name" in msg:
            result["name"] = msg["name"]
        if "user_id" in msg:
            result["user_id"] = msg["user_id"]
        if "timestamp" in msg:
            result["timestamp"] = msg["timestamp"]

        return result

    def _build_safe_history(self, messages: list) -> list:
        """构建安全的聊天历史，过滤掉工具调用相关的消息。"""
        safe = []
        for msg in messages:
            if msg.get("role") == "tool":
                continue
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                continue
            clean = dict(msg)
            clean.pop("tool_calls", None)
            safe.append(clean)
        return safe

    @staticmethod
    def _parse_pb_close_session(raw_text: str) -> tuple[str, bool]:
        """
        解析人格脑输出中的 close_session 元数据标志。

        PB 输出格式约定：正文中可包含如下 JSON 代码块：
            ```session_ctl
            {"close_session": true}
            ```

        返回 (清理后的文本, close_session 标志)。
        """
        import re
        close_session = False
        clean_text = raw_text

        # 匹配 ```session_ctl ... ``` 代码块
        pattern = re.compile(r'```session_ctl\s*\n?\s*(\{.*?\})\s*\n?\s*```', re.DOTALL)
        match = pattern.search(raw_text)
        if match:
            try:
                ctl = json.loads(match.group(1))
                close_session = bool(ctl.get("close_session", False))
            except (json.JSONDecodeError, ValueError):
                pass
            # 从输出文本中移除控制块
            clean_text = pattern.sub('', raw_text).strip()

        return clean_text, close_session

    @staticmethod
    def _build_logic_history(messages: list, max_tokens: int = 4000) -> list:
        """
        为逻辑脑构建纯净历史（话题级全量 + XML 封印 + Token 预算截断）。

        - user / tool_calls / tool → 原样保留
        - 纯文本 assistant → 用 <actor_past_reply> 封印，防止 RP 污染
        - 逆向遍历，优先保留最新上下文，达到 Token 阈值后停止
        """
        from ..engine.token_budget import estimate_tokens

        result = []
        current_tokens = 0

        for msg in reversed(messages):
            role = msg.get("role")
            safe_msg = None

            if role == "user":
                safe_msg = msg
            elif role == "assistant" and msg.get("tool_calls"):
                safe_msg = msg
            elif role == "tool":
                safe_msg = msg
            elif role == "assistant" and msg.get("content"):
                # 封印 RP 文本：让逻辑脑知道 Bot 说过什么，但不会被语气污染
                safe_msg = msg.copy()
                safe_msg["content"] = (
                    f"<actor_past_reply>\n{msg['content']}\n</actor_past_reply>"
                )

            if safe_msg:
                msg_text = str(safe_msg.get("content", "")) + str(safe_msg.get("tool_calls", ""))
                tokens = estimate_tokens(msg_text)
                if current_tokens + tokens > max_tokens:
                    break
                result.append(safe_msg)
                current_tokens += tokens

        # 翻转回正序
        result.reverse()
        return result

    async def _run_actor_only(
        self,
        user_id: str,
        text: str,
        context: Dict[str, Any],
        safe_history: list,
        snapshot: Dict[str, Any],
        system_notification: str = "",
        worldbook_entries: str = "",
    ) -> Dict[str, Any]:
        """仅演员脑渲染：编译 prompt → LLM（无 tools）→ 返回文本。"""
        lorebook_context = {
            "group_id": context.get("group_id", 0),
            "active_uids": list({m.get("user_id") for m in safe_history if m.get("user_id")}),
            "token_arbitration_enabled": plugin_config.token_arbitration_enabled,
            "_matched_rule": context.get("_matched_rule"),
        }

        compiled_messages = self.prompt_adapter.compile_actor_prompt(
            chat_history=safe_history,
            snapshot=snapshot,
            context=lorebook_context,
            system_notification=system_notification,
            worldbook_entries=worldbook_entries,
        )
        msg = await self._call_llm(compiled_messages, tools=None, temperature=0.7)
        if msg is None:
            return {"text": "大脑短路了，等一下再试吧...", "images": []}
        return {"text": msg.get("content", ""), "images": []}

    async def _handle_forced_tool(
        self,
        text: str,
        context: Dict[str, Any],
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        硬指令前缀路由：若用户输入匹配 force_tool_prefixes 配置的前缀，
        跳过逻辑脑直接执行工具，返回 system_notification 供演员脑使用。
        返回 None 表示未匹配。
        """
        prefixes = plugin_config.force_tool_prefixes
        if not prefixes:
            return None

        text_stripped = text.strip()
        for prefix, tool_name in prefixes.items():
            if not text_stripped.startswith(prefix):
                continue

            remainder = text_stripped[len(prefix):].strip()
            tool_obj = self.registry.get_tool(tool_name)
            if not tool_obj:
                logger.warning(f"[Agent] 硬指令路由: 工具 '{tool_name}' 未注册")
                return None

            # 权限校验
            perm_srv = context.get("permission_service")
            user_id = context.get("user_id", "")
            is_admin = context.get("is_admin", False)
            schemas = self.registry.get_all_schemas(perm_srv, user_id, is_admin)
            if not any(s["function"]["name"] == tool_name for s in schemas):
                logger.info(f"[Agent] 硬指令路由: 用户 {user_id} 无权使用工具 '{tool_name}'")
                return None

            # 参数提取
            args = self._build_prefix_args(tool_name, remainder, tool_obj)

            logger.warning(f"[AUDIT_FAST_TRACK] 触发硬指令短路! 用户: {context.get('user_id')}, 前缀: '{prefix}', 工具: {tool_name}, 参数: {args}")

            try:
                result_text, images = await asyncio.wait_for(
                    self.registry.execute_tool(tool_name, args, context),
                    timeout=TOOL_EXEC_TIMEOUT,
                )
            except asyncio.TimeoutError:
                result_text = f"工具 '{tool_name}' 执行超时。"
                images = []
            except Exception as e:
                logger.error(f"[AUDIT_TOOL_ERROR] 工具 '{tool_name}' 执行崩溃! 错误详情: {e}")
                result_text = f"工具 '{tool_name}' 执行出错: {e}"
                images = []

            # 审计日志（仅写操作）
            _forced_tool_obj = self.registry.get_tool(tool_name)
            if _forced_tool_obj and _forced_tool_obj.is_write_operation:
                has_error = any(k in result_text for k in ("Error", "error", "Exception", "失败", "超时"))
                await self.repo.insert_tool_log(
                    session_id=session_id,
                    request_id=uuid.uuid4().hex,
                    step=1,
                    trigger="forced_shortcut",
                    tool_name=tool_name,
                    arguments=args,
                    result_summary=result_text[:300],
                    error=result_text[:300] if has_error else None,
                )

            log_entry = f"[{tool_name}] {result_text[:TOOL_RESULT_MAX_LEN]}"
            return {
                "system_notification": f"[SYSTEM_TOOL_RESULT] 以下是工具执行结果：\n{log_entry}",
                "images": images,
            }

        return None

    @staticmethod
    def _build_prefix_args(tool_name: str, remainder: str, tool_obj) -> dict:
        """根据工具 schema 和前缀后的剩余文本构建参数。"""
        params = (tool_obj.parameters.get("properties") or {})

        if tool_name == "jm_download" and remainder:
            ids = [x.strip() for x in remainder.replace(",", " ").split() if x.strip()]
            return {"ids": ids}

        if tool_name in ("search_acg_image", "generate_image") and remainder:
            if "keywords" in params:
                return {"keywords": remainder}
            if "prompt" in params:
                return {"prompt": remainder}

        if tool_name == "search_acg_image":
            return {}

        if tool_name == "generate_image" and remainder:
            return {"prompt": remainder}

        return {}

    async def run_agent(
        self,
        user_id: str,
        text: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        双脑模式主入口。
        - enable_dual_brain=False → 回退到 _run_agent_single_brain
        - source_type="system" → 系统级短路，无 DB 写入
        - 正常流程：Phase 1 逻辑循环 → Phase 2 人格渲染
        """
        start_time = time.time()

        # ---- 0. 单脑回退 ----
        if not plugin_config.enable_dual_brain:
            return await self._run_agent_single_brain(user_id, text, context)

        group_id = context.get("group_id", 0)
        session_id = self._build_session_id(group_id, user_id)

        # ---- 1. 系统级短路（无 DB 写入） ----
        if context.get("source_type") == "system":
            context.setdefault("_agent_state", {"step": "init"})
            context["_agent_state"]["step"] = "system_event"
            try:
                msgs = await asyncio.wait_for(
                    self.repo.get_recent_messages(session_id, limit=RECENT_MESSAGES_LIMIT),
                    timeout=DB_QUERY_TIMEOUT,
                )
            except asyncio.TimeoutError:
                msgs = []

            safe_history = []  # 系统级事件隔离群聊历史，防止被历史上下文污染

            # 旧的 memorySnapshot 注入已废弃
            snapshot = {}
            wb_content = self.world_book.search(text, group_id)
            result = await self._run_actor_only(
                user_id, text, context, safe_history, snapshot,
                system_notification=text,
                worldbook_entries=wb_content,
            )
            latency = round((time.time() - start_time) * 1000, 2)
            asyncio.create_task(SessionDumper.dump(
                group_id=str(context.get("group_id", "0")),
                user_id=user_id,
                payload={
                    "input_text": text,
                    "agent_state": "system_event",
                    "matched_rule": None,
                    "tool_logs": [],
                    "output_text": result.get("text", ""),
                    "has_images": bool(result.get("images")),
                    "latency_ms": latency,
                },
            ))
            return result

        # ---- 2. 规则匹配 + 落库 ----
        context.setdefault("_agent_state", {"step": "init"})
        context['_tool_executed'] = False
        await self.rule_engine.match(text, context)
        context["_agent_state"]["step"] = "matched"

        topic_id = context.get("topic_id")

        sender_name = context.get("sender_name", "User")
        now_iso = datetime.now().isoformat()
        user_fingerprint = context.get("message_fingerprint")
        await self.repo.add_message(
            session_id=session_id,
            role="user",
            content=text,
            topic_id=topic_id,
            user_id=user_id,
            name=sender_name,
            timestamp=now_iso,
            message_fingerprint=user_fingerprint,
        )

        # ---- 3. 读取消息（话题级精准查询 or 全局回退） ----
        if topic_id:
            try:
                recent_msgs = await asyncio.wait_for(
                    self.repo.get_messages_by_topic(topic_id),
                    timeout=DB_QUERY_TIMEOUT,
                )
                # 话题内消息过多时截断，防止 OOM
                if len(recent_msgs) > 50:
                    recent_msgs = recent_msgs[-50:]
            except asyncio.TimeoutError:
                logger.error(f"[Agent] 话题 {topic_id} 消息查询超时，降级为空历史")
                recent_msgs = []
        else:
            # 私聊或无话题 ID 时回退到全局查询
            try:
                recent_msgs = await asyncio.wait_for(
                    self.repo.get_recent_messages(session_id, limit=RECENT_MESSAGES_LIMIT),
                    timeout=DB_QUERY_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error(f"[Agent] 获取最近消息超时（{DB_QUERY_TIMEOUT}s），降级为空历史")
                recent_msgs = []

        messages = [m for m in (self._to_openai_message(x) for x in recent_msgs) if m is not None]

        active_uids = list({m.get("user_id") for m in recent_msgs if m.get("user_id")})

        # 工具 schema
        perm_srv = context.get("permission_service")
        is_admin = context.get("is_admin", False)
        tools = self.registry.get_all_schemas(perm_srv, user_id, is_admin)

        # 显式触发时，从工具列表中移除 exit_session（防止逻辑脑误判退出）
        if context.get("is_tome") or context.get("is_reply_bot") or context.get("has_wake_word"):
            tools = [t for t in tools if t["function"]["name"] != "exit_session"]

        logger.info(
            f"[Agent] 逻辑脑上下文: is_tome={context.get('is_tome')}, "
            f"is_reply_bot={context.get('is_reply_bot')}, has_wake_word={context.get('has_wake_word')}, "
            f"tools_count={len(tools)}, tools={[t['function']['name'] for t in tools]}"
        )

        # 语义检索
        semantic_hits = []
        if plugin_config.semantic_lorebook_enabled and self.semantic_lorebook and text:
            try:
                semantic_hits = await asyncio.wait_for(
                    self.semantic_lorebook.search(text, top_k=3),
                    timeout=SEMANTIC_SEARCH_TIMEOUT,
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[Agent] 语义检索失败，降级为纯关键词: {e}")
                semantic_hits = []

        # 旧的 memorySnapshot 注入已废弃（group_memory/group_dynamics 不再注入 Actor Prompt）
        snapshot = {}

        # 世界书关键词检索（供演员脑注入）
        wb_content = self.world_book.search(text, group_id)

        # 构建安全历史（剔除 tool 相关）供逻辑脑使用
        safe_history = self._build_safe_history(messages)

        # ---- 3.5. 硬指令前缀快速路由 ----
        forced_result = await self._handle_forced_tool(text, context, session_id)
        if forced_result is not None:
            context["_agent_state"]["step"] = "forced_actor"
            actor_lorebook_context = {
                "group_id": group_id,
                "active_uids": active_uids,
                "token_arbitration_enabled": plugin_config.token_arbitration_enabled,
            }
            actor_msgs = self.prompt_adapter.compile_actor_prompt(
                chat_history=safe_history,
                snapshot=snapshot,
                context=actor_lorebook_context,
                system_notification=forced_result["system_notification"],
                worldbook_entries=wb_content,
            )
            final_msg = await self._call_llm(
                actor_msgs, tools=None, model=plugin_config.deepseek_model_name, temperature=0.7,
            )
            final_text = final_msg.get("content", "") if final_msg else "任务完成。"

            await self.repo.add_message(
                session_id=session_id,
                role="assistant",
                content=final_text,
                topic_id=topic_id,
                timestamp=datetime.now().isoformat(),
                message_fingerprint=uuid.uuid4().hex,
            )

            latency = round((time.time() - start_time) * 1000, 2)
            asyncio.create_task(SessionDumper.dump(
                group_id=str(group_id),
                user_id=user_id,
                payload={
                    "input_text": text,
                    "agent_state": "forced_actor",
                    "matched_rule": (context.get("_matched_rule") or {}).get("rule_name"),
                    "tool_logs": [forced_result["system_notification"][:200]],
                    "output_text": final_text,
                    "has_images": bool(forced_result["images"]),
                    "latency_ms": latency,
                },
            ))
            return {"text": final_text, "images": forced_result["images"]}

        # ---- 4. Phase 1：逻辑循环 ----
        context["_agent_state"]["step"] = "executing"
        # 防线 1：纯净历史 — 话题级全量 + XML 封印 + Token 预算截断
        logic_history = self._build_logic_history(messages)
        lorebook_context = {
            "group_id": group_id,
            "active_uids": active_uids,
            "token_arbitration_enabled": plugin_config.token_arbitration_enabled,
            "_matched_rule": context.get("_matched_rule"),
            "is_tome": context.get("is_tome"),
            "is_reply_bot": context.get("is_reply_bot"),
            "has_wake_word": context.get("has_wake_word"),
        }
        if semantic_hits:
            lorebook_context["semantic_hits"] = semantic_hits

        worldbook_entries = ""
        if semantic_hits:
            worldbook_entries = "\n".join(h.get("content", "") for h in semantic_hits if h.get("content"))

        logic_msgs = self.prompt_adapter.compile_logic_prompt(
            logic_history, snapshot, lorebook_context, tools=tools, worldbook_entries=worldbook_entries,
        )

        tool_execution_logs: list[str] = []
        all_images: list[str] = []
        executed_signatures: set[str] = set()
        logic_model = plugin_config.logic_model_name or plugin_config.deepseek_model_name
        max_loops = plugin_config.agent_max_loops
        request_id = uuid.uuid4().hex
        step_counter = 0

        # 初始化请求级防重放计数器
        self.registry.begin_request(request_id)
        context["request_id"] = request_id

        # 逻辑脑终态短路标志
        lb_exit_session = False
        lb_terminal_hit = False

        for loop_count in range(max_loops):
            logger.info(f"[Agent] 逻辑脑调用: model={logic_model}")
            msg = await self._call_llm(
                logic_msgs, model=logic_model, temperature=0.0,
            )
            if msg is None:
                break

            assistant_content = msg.get("content", "")
            logger.info(f"[Agent] 逻辑脑返回: {assistant_content}")

            # XML 解析逻辑：从 <invoke> 标签中提取工具调用
            tcs = []
            invoke_match = re.search(r'<invoke\s+name="([^"]+)">\s*(.*?)\s*</invoke>', assistant_content, re.DOTALL)
            if invoke_match:
                func_name = invoke_match.group(1)
                params_block = invoke_match.group(2)
                arguments = {}
                if params_block:
                if params_block:
                    param_matches = re.finditer(
                        r'<parameter\s+name="([^"]+)">\s*(.*?)\s*
