# src/plugins/chatbot/services/agent_service.py

import asyncio
import json
import uuid
import httpx
from datetime import datetime
from typing import Dict, Any, Optional
from nonebot.log import logger

from ..config import plugin_config
from .prompt_adapter import PromptAdapter
from .rule_engine import RuleEngine, RuleEngineCore, SQLiteRuleProvider
from ..repositories.memory_repo import MemoryRepository
from ..repositories.rule_repo import RuleRepository
from ..tools.registry import ToolRegistry
from ..tools.image_tool import GenerateImageTool, SearchAcgImageTool
from ..tools.admin_tool import BanUserTool
from ..tools.book_tool import RecommendBookTool, JmDownloadTool
from ..tools.system_tool import MarkTaskCompleteTool
from ..tools.rule_tool import LearnRuleTool, ForgetRuleTool
from .memory_service import MemoryService
from ..utils.embedding import create_semantic_lorebook
from ..utils.alert_manager import send_emergency_alert, reset_cooldown

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
        self.registry = ToolRegistry()
        self.http_client = httpx.AsyncClient()
        self.semantic_lorebook = create_semantic_lorebook(plugin_config)
        self.rule_engine = RuleEngine(SQLiteRuleProvider())
        self.rule_repo = RuleRepository()
        self._register_tools()

    def _register_tools(self):
        self.registry.register(GenerateImageTool())
        self.registry.register(SearchAcgImageTool())
        self.registry.register(BanUserTool())
        self.registry.register(RecommendBookTool())
        self.registry.register(JmDownloadTool())
        self.registry.register(LearnRuleTool())
        self.registry.register(ForgetRuleTool())
        if plugin_config.enable_dynamic_loop:
            self.registry.register(MarkTaskCompleteTool())

    async def close(self):
        await self.http_client.aclose()

    async def _call_llm(
        self,
        messages: list,
        model: str = None,
        tools: list = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Optional[Dict[str, Any]]:
        """
        统一 LLM 调用入口。
        - R1/reasoner 模型自动清除 tools 并跳过 thinking 参数。
        - 402/403 触发紧急告警。
        - 返回 choices[0].message 字典，失败返回 None。
        """
        model = model or plugin_config.deepseek_model_name

        # R1/reasoner 模型不支持 Function Calling 和 thinking 参数
        is_reasoner = "reasoner" in model.lower() or "r1" in model.lower()
        if is_reasoner and tools:
            logger.warning(f"[Agent] 模型 '{model}' 不支持 Function Calling，已清除 tools")
            tools = None

        api_payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not is_reasoner:
            api_payload["thinking"] = {"type": "disabled"}
        if tools:
            api_payload["tools"] = tools

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
            reset_cooldown()
        except Exception as e:
            logger.error(f"[Agent] 请求 LLM 失败: {e}")
            return None

        choices = data.get("choices", [])
        if not choices:
            return None
        return choices[0].get("message", {})

    @staticmethod
    def _build_session_id(group_id: int, user_id: str) -> str:
        if group_id and int(group_id) != 0:
            return f"group_{group_id}"
        return f"private_{user_id}"

    @staticmethod
    def _to_openai_message(msg: Dict[str, Any]) -> Dict[str, Any]:
        """
        将数据库读出的消息字典转换为 OpenAI 兼容格式。
        透传 name、user_id 保证群聊多角色区分。
        """
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
            
        # --- 修复逻辑：正确处理 tool_calls 和 tool_call_id ---
        tool_data = msg.get("tool_calls")
        if tool_data:
            if msg["role"] == "tool" and isinstance(tool_data, dict):
                # 从我们存入的字典中提取工具调用 ID
                result["tool_call_id"] = tool_data.get("tool_call_id", "")
            else:
                result["tool_calls"] = tool_data
                
        # 兼容处理：防止因为极端情况下的脏数据再次导致崩溃
        if msg["role"] == "tool" and "tool_call_id" not in result:
            result["tool_call_id"] = "fallback_id_to_prevent_crash"
            
        return result

    def _build_memory_snapshot(
        self,
        existing_summary: str,
        profiles_raw: Dict,
        decayed_relations: list,
    ) -> Dict[str, Any]:
        """从数据库原始数据构建 memorySnapshot 字典。"""
        return {
            "summary": existing_summary,
            "profiles": [
                {
                    "user_id": uid,
                    "traits": [
                        {"content": t["content"], "confidence": t.get("confidence", 0.5)}
                        for t in traits
                    ]
                }
                for uid, traits in profiles_raw.items()
            ],
            "relations": [
                {
                    "relation_id": r["relation_id"],
                    "subject_entity": r["subject_entity"],
                    "predicate": r["predicate"],
                    "object_entity": r["object_entity"],
                    "confidence": r["decayed_confidence"],
                }
                for r in decayed_relations
            ],
        }

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

    async def _run_actor_only(
        self,
        user_id: str,
        text: str,
        context: Dict[str, Any],
        safe_history: list,
        snapshot: Dict[str, Any],
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
        )
        msg = await self._call_llm(compiled_messages, tools=None, temperature=0.7)
        if msg is None:
            return {"text": "大脑短路了，等一下再试吧...", "images": []}
        return {"text": msg.get("content", ""), "images": []}

    async def _try_forced_exec(
        self,
        context: Dict[str, Any],
        text: str,
        session_id: str,
        tool_execution_logs: list,
        all_images: list,
        logic_msgs: list,
    ):
        """兜底强制执行：若规则匹配但逻辑脑未执行工具，则强制执行。"""
        matched_rule = context.get('_matched_rule')
        if not matched_rule or context.get('_tool_executed'):
            return

        tool_name = matched_rule.get('tool_name', '')
        tool_obj = self.registry._tools.get(tool_name)
        if not tool_obj or tool_obj.risk_level != 'low' or not tool_obj.allow_forced_exec:
            return

        forced_args = RuleEngineCore.extract_args(matched_rule, text)
        if forced_args is None:
            return

        forced_id = f"forced_{matched_rule.get('rule_id', 'unknown')}_{uuid.uuid4().hex[:8]}"
        try:
            result_text, images = await asyncio.wait_for(
                self.registry.execute_tool(tool_name, forced_args, context),
                timeout=TOOL_EXEC_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[Agent] 兜底工具 '{tool_name}' 超时")
            result_text = f"工具 '{tool_name}' 执行超时。"
            images = []
        except Exception as e:
            logger.error(f"[Agent] 兜底工具 '{tool_name}' 异常: {e}")
            result_text = f"工具 '{tool_name}' 执行出错: {e}"
            images = []

        all_images.extend(images)
        context['_tool_executed'] = True

        log_entry = f"[{tool_name}] {result_text[:TOOL_RESULT_MAX_LEN]}"
        tool_execution_logs.append(log_entry)

        # 持久化伪造的 assistant + tool 消息对
        forged_assistant = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": forced_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(forced_args),
                },
            }],
        }
        forged_tool = {
            "role": "tool",
            "tool_call_id": forced_id,
            "name": tool_name,
            "content": result_text,
        }
        logic_msgs.append(forged_assistant)
        logic_msgs.append(forged_tool)

        await self.repo.add_message(
            session_id=session_id,
            role="assistant",
            content="",
            timestamp=datetime.now().isoformat(),
            tool_calls=forged_assistant["tool_calls"],
        )
        await self.repo.add_message(
            session_id=session_id,
            role="tool",
            content=result_text,
            timestamp=datetime.now().isoformat(),
            name=tool_name,
            tool_calls={"tool_call_id": forced_id},
        )

        logger.info(f"[Agent] 兜底执行: {tool_name} args={forced_args}")

    async def run_agent(
        self,
        user_id: str,
        text: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        双脑模式主入口。
        - enable_dual_brain=False → 回退到 _run_agent_single_brain
        - user_id="system_welcome" → 系统级短路，无 DB 写入
        - 正常流程：Phase 1 逻辑循环 → Phase 2 人格渲染
        """
        # ---- 0. 单脑回退 ----
        if not plugin_config.enable_dual_brain:
            return await self._run_agent_single_brain(user_id, text, context)

        group_id = context.get("group_id", 0)
        session_id = self._build_session_id(group_id, user_id)

        # ---- 1. 系统级短路（无 DB 写入） ----
        if user_id == "system_welcome":
            try:
                msgs = await asyncio.wait_for(
                    self.repo.get_recent_messages(session_id, limit=RECENT_MESSAGES_LIMIT),
                    timeout=DB_QUERY_TIMEOUT,
                )
            except asyncio.TimeoutError:
                msgs = []

            safe_history = [self._to_openai_message(m) for m in msgs]
            safe_history.append({
                "role": "user",
                "content": text,
                "name": "System",
            })

            # 读取已有记忆，但不写入
            try:
                snapshot = await asyncio.wait_for(
                    self.repo.get_memory_snapshot(session_id),
                    timeout=DB_QUERY_TIMEOUT,
                )
            except asyncio.TimeoutError:
                snapshot = {"summary": "", "profiles": [], "relations": []}
            return await self._run_actor_only(user_id, text, context, safe_history, snapshot)

        # ---- 2. 规则匹配 + 落库 ----
        context['_tool_executed'] = False
        await self.rule_engine.match(text, context)

        sender_name = context.get("sender_name", "User")
        now_iso = datetime.now().isoformat()
        await self.repo.add_message(
            session_id=session_id,
            role="user",
            content=text,
            user_id=user_id,
            name=sender_name,
            timestamp=now_iso,
        )

        # ---- 3. 读取近期消息（此时包含刚写入的用户消息） ----
        try:
            recent_msgs = await asyncio.wait_for(
                self.repo.get_recent_messages(session_id, limit=RECENT_MESSAGES_LIMIT),
                timeout=DB_QUERY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"[Agent] 获取最近消息超时（{DB_QUERY_TIMEOUT}s），降级为空历史")
            recent_msgs = []

        messages = [self._to_openai_message(m) for m in recent_msgs]

        # 活跃画像
        try:
            active_uids = list({m.get("user_id") for m in recent_msgs if m.get("user_id")})
            profiles_raw = await asyncio.wait_for(
                self.repo.get_active_profiles(session_id, active_uids),
                timeout=DB_QUERY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("[Agent] 获取活跃画像超时，降级为空画像")
            profiles_raw = {}
            active_uids = []

        # 群组摘要
        try:
            existing_summary = await asyncio.wait_for(
                self.repo.get_group_summary(session_id),
                timeout=DB_QUERY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("[Agent] 获取群组摘要超时，降级为空摘要")
            existing_summary = ""

        # 关系图谱
        decayed_relations = []
        if plugin_config.entity_relation_enabled:
            try:
                entity_ids = [f"user_{uid}" for uid in active_uids if uid]
                decayed_relations = await asyncio.wait_for(
                    self.repo.get_relations_with_decay(
                        session_id, entity_ids=entity_ids if entity_ids else None
                    ),
                    timeout=DB_QUERY_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error("[Agent] 获取关系图谱超时，降级为空关系")
                decayed_relations = []

        # 工具 schema
        perm_srv = context.get("permission_service")
        is_admin = context.get("is_admin", False)
        tools = self.registry.get_all_schemas(perm_srv, user_id, is_admin)

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

        snapshot = self._build_memory_snapshot(existing_summary, profiles_raw, decayed_relations)

        # 构建安全历史（剔除 tool 相关）供逻辑脑使用
        safe_history = self._build_safe_history(messages)

        # ---- 4. Phase 1：逻辑循环 ----
        logic_history = safe_history[-LOGIC_HISTORY_LIMIT:]
        lorebook_context = {
            "group_id": group_id,
            "active_uids": active_uids,
            "token_arbitration_enabled": plugin_config.token_arbitration_enabled,
            "_matched_rule": context.get("_matched_rule"),
        }
        if semantic_hits:
            lorebook_context["semantic_hits"] = semantic_hits

        worldbook_entries = ""
        if semantic_hits:
            worldbook_entries = "\n".join(h.get("content", "") for h in semantic_hits if h.get("content"))

        logic_msgs = self.prompt_adapter.compile_logic_prompt(
            logic_history, snapshot, lorebook_context, worldbook_entries=worldbook_entries,
        )

        tool_execution_logs: list[str] = []
        all_images: list[str] = []
        executed_signatures: set[str] = set()
        logic_model = plugin_config.logic_model_name or plugin_config.deepseek_model_name
        max_loops = plugin_config.agent_max_loops

        for loop_count in range(max_loops):
            msg = await self._call_llm(
                logic_msgs, tools=tools, model=logic_model, temperature=0.0,
            )
            if msg is None:
                break

            tcs = msg.get("tool_calls", [])
            if not tcs:
                break

            # 去重：计算签名，若全部已执行则终止
            new_tcs = []
            for tc in tcs:
                func_name = tc.get("function", {}).get("name", "")
                args_str = tc.get("function", {}).get("arguments", "{}")
                sig = f"{func_name}_{args_str}"
                if sig not in executed_signatures:
                    executed_signatures.add(sig)
                    new_tcs.append(tc)

            if not new_tcs:
                logger.info(f"[Agent] {session_id} 逻辑循环：所有工具调用已重复，退出")
                break

            # 追加 assistant 消息（含全部 tool_calls）到 logic_msgs 并持久化
            assistant_content = msg.get("content", "")
            logic_msgs.append({
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": tcs,
            })
            await self.repo.add_message(
                session_id=session_id,
                role="assistant",
                content=assistant_content,
                timestamp=datetime.now().isoformat(),
                tool_calls=tcs,
            )

            # 遍历执行工具
            for tc in tcs:
                tool_id = tc.get("id", "")
                func_name = tc.get("function", {}).get("name", "")
                arguments_str = tc.get("function", {}).get("arguments", "{}")
                try:
                    arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
                except json.JSONDecodeError:
                    arguments = {}

                try:
                    result_text, images = await asyncio.wait_for(
                        self.registry.execute_tool(func_name, arguments, context),
                        timeout=TOOL_EXEC_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[Agent] 工具 '{func_name}' 超时（{TOOL_EXEC_TIMEOUT}s）")
                    result_text = f"Error: Tool '{func_name}' execution timed out."
                    images = []
                except Exception as e:
                    logger.error(f"[Agent] 工具 '{func_name}' 异常: {e}")
                    result_text = f"Error: Tool '{func_name}' failed: {e}"
                    images = []

                all_images.extend(images)

                # 记录日志（截断）
                log_entry = f"[{func_name}] {result_text[:TOOL_RESULT_MAX_LEN]}"
                tool_execution_logs.append(log_entry)

                # 追加 tool 结果到 logic_msgs 并持久化
                tool_timestamp = datetime.now().isoformat()
                logic_msgs.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": func_name,
                    "content": result_text,
                })
                await self.repo.add_message(
                    session_id=session_id,
                    role="tool",
                    content=result_text,
                    timestamp=tool_timestamp,
                    name=func_name,
                    tool_calls={"tool_call_id": tool_id},
                )

                # 如果工具执行出错，向逻辑脑注入一条系统级警告
                if any(k in result_text for k in ("Error", "error", "Exception", "失败", "超时")):
                    logic_msgs.append({
                        "role": "system",
                        "content": (
                            "[系统警告] 上一次工具调用失败。"
                            "如果无法通过更换参数解决，请立即停止调用工具，跳出循环。"
                        ),
                    })

        # ---- 5. 兜底强制执行 ----
        await self._try_forced_exec(
            context, text, session_id, tool_execution_logs, all_images, logic_msgs,
        )

        # ---- 6. Phase 2：人格渲染 ----
        if tool_execution_logs:
            system_notification = "以下是工具执行结果：\n" + "\n".join(tool_execution_logs)
        else:
            system_notification = ""

        actor_lorebook_context = {
            "group_id": group_id,
            "active_uids": active_uids,
            "token_arbitration_enabled": plugin_config.token_arbitration_enabled,
            "_matched_rule": context.get("_matched_rule"),
        }

        actor_msgs = self.prompt_adapter.compile_actor_prompt(
            chat_history=safe_history,
            snapshot=snapshot,
            context=actor_lorebook_context,
            system_notification=system_notification,
        )
        final_msg = await self._call_llm(
            actor_msgs, tools=None, model=plugin_config.deepseek_model_name, temperature=0.7,
        )
        final_text = final_msg.get("content", "") if final_msg else ""
        if not final_text:
            # 兜底：从逻辑循环的最后一条 assistant 消息取内容
            last_assistant = next(
                (m for m in reversed(logic_msgs) if m.get("role") == "assistant" and m.get("content")),
                None,
            )
            final_text = last_assistant.get("content", "") if last_assistant else "任务完成。"

        # 持久化最终 assistant 回复（不含 tool_calls）
        await self.repo.add_message(
            session_id=session_id,
            role="assistant",
            content=final_text,
            timestamp=datetime.now().isoformat(),
        )

        # 命中统计
        matched_rule = context.get('_matched_rule')
        if matched_rule and context.get('_tool_executed'):
            try:
                await self.rule_repo.increment_hit_count(matched_rule['rule_id'])
            except Exception as e:
                logger.warning(f"[Agent] 更新命中统计失败: {e}")

        # 记忆压缩（熔断器保护）
        from .. import circuit_breaker
        if circuit_breaker is None or circuit_breaker.allow_new_task():
            asyncio.create_task(
                self.memory_service.process_session_memory(session_id)
            )
        else:
            logger.debug("[Agent] 记忆压缩熔断中，跳过入队")

        return {"text": final_text, "images": all_images}

    async def _run_agent_single_brain(
        self,
        user_id: str,
        text: str,
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """旧版单脑模式完整流程（保留用于 enable_dual_brain=False 回退）。"""
        # ---------- 1. 会话身份 ----------
        group_id = context.get("group_id", 0)
        session_id = self._build_session_id(group_id, user_id)

        # ---------- 2. 规则匹配（一次请求只调用一次） ----------
        context['_tool_executed'] = False
        await self.rule_engine.match(text, context)

        # ---------- 3. 第一时间落库：记录 User 消息 ----------
        sender_name = context.get("sender_name", "User")
        now_iso = datetime.now().isoformat()

        await self.repo.add_message(
            session_id=session_id,
            role="user",
            content=text,
            user_id=user_id,
            name=sender_name,
            timestamp=now_iso,
        )

        # ---------- 3. 精准备料（含超时降级） ----------
        # 3a. 最近消息
        try:
            recent_msgs = await asyncio.wait_for(
                self.repo.get_recent_messages(session_id, limit=RECENT_MESSAGES_LIMIT),
                timeout=DB_QUERY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"[Agent] 获取最近消息超时（{DB_QUERY_TIMEOUT}s），降级为空历史")
            recent_msgs = []
        messages = [self._to_openai_message(m) for m in recent_msgs]

        # 3b. 活跃画像
        try:
            active_uids = list({
                m.get("user_id") for m in recent_msgs if m.get("user_id")
            })
            profiles_raw = await asyncio.wait_for(
                self.repo.get_active_profiles(session_id, active_uids),
                timeout=DB_QUERY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("[Agent] 获取活跃画像超时，降级为空画像")
            profiles_raw = {}
            active_uids = []

        # 3c. 群组摘要
        try:
            existing_summary = await asyncio.wait_for(
                self.repo.get_group_summary(session_id),
                timeout=DB_QUERY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("[Agent] 获取群组摘要超时，降级为空摘要")
            existing_summary = ""

        # 3d. 关系图谱（如果启用）
        decayed_relations = []
        if plugin_config.entity_relation_enabled:
            try:
                entity_ids = [f"user_{uid}" for uid in active_uids if uid]
                decayed_relations = await asyncio.wait_for(
                    self.repo.get_relations_with_decay(
                        session_id, entity_ids=entity_ids if entity_ids else None
                    ),
                    timeout=DB_QUERY_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error("[Agent] 获取关系图谱超时，降级为空关系")
                decayed_relations = []

        # 工具 schema
        perm_srv = context.get("permission_service")
        is_admin = context.get("is_admin", False)
        tools = self.registry.get_all_schemas(perm_srv, user_id, is_admin)

        max_loops = plugin_config.agent_max_loops
        all_images = []
        last_assistant_text = ""
        final_text = ""

        # ---------- 4. ReAct 循环（智能终止） ----------
        for loop_count in range(max_loops):
            # ── 兜底：步数耗尽 ──
            if loop_count >= max_loops:
                logger.info(f"[Agent] {session_id} 达到最大步数 {max_loops}，退出循环")
                break

            # 构建群聊上下文（供世界书感知过滤使用）
            active_uids = list({
                m.get("user_id") for m in messages if m.get("user_id")
            })
            lorebook_context = {
                "group_id": group_id,
                "active_uids": active_uids,
                "token_arbitration_enabled": plugin_config.token_arbitration_enabled,
                "_matched_rule": context.get("_matched_rule"),
            }

            # 语义向量检索（降级安全：超时或失败返回空列表）
            if plugin_config.semantic_lorebook_enabled and self.semantic_lorebook and text:
                try:
                    semantic_hits = await asyncio.wait_for(
                        self.semantic_lorebook.search(text, top_k=3),
                        timeout=SEMANTIC_SEARCH_TIMEOUT,
                    )
                    lorebook_context["semantic_hits"] = semantic_hits
                except asyncio.TimeoutError:
                    logger.warning(
                        "[Agent] 语义检索超时（%ds），降级为纯关键词匹配",
                        SEMANTIC_SEARCH_TIMEOUT,
                    )
                    lorebook_context["semantic_hits"] = []
                except Exception as e:
                    logger.warning(f"[Agent] 语义检索失败，降级为纯关键词: {e}")
                    lorebook_context["semantic_hits"] = []

            # 构建 memorySnapshot
            memory_snapshot = {
                "summary": existing_summary,
                "profiles": [
                    {
                        "user_id": uid,
                        "traits": [
                            {"content": t["content"], "confidence": t.get("confidence", 0.5)}
                            for t in traits
                        ]
                    }
                    for uid, traits in profiles_raw.items()
                ],
                "relations": [
                    {
                        "relation_id": r["relation_id"],
                        "subject_entity": r["subject_entity"],
                        "predicate": r["predicate"],
                        "object_entity": r["object_entity"],
                        "confidence": r["decayed_confidence"],
                    }
                    for r in decayed_relations
                ],
            }

            # 使用 PromptAdapter 组装 messages（engine 管线）
            compiled_messages = self.prompt_adapter.compile_actor_prompt(
                chat_history=messages,
                snapshot=memory_snapshot,
                context=lorebook_context,
            )

            # 直连 LLM API
            msg = await self._call_llm(compiled_messages, tools=tools)
            if msg is None:
                return {"text": "大脑短路了，等一下再试吧...", "images": all_images}

            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])

            # ── 重复检测：连续两轮高度相似则终止（需开启动态循环） ──
            if plugin_config.enable_dynamic_loop and content and last_assistant_text:
                similarity = _jaccard(content, last_assistant_text)
                if similarity > JACCARD_THRESHOLD:
                    logger.info(
                        f"[Agent] {session_id} 检测到重复输出 (sim={similarity:.2f})，退出循环"
                    )
                    final_text = content
                    break

            if content:
                last_assistant_text = content

            # ---------- 5. 持久化 Assistant 消息 ----------
            assistant_timestamp = datetime.now().isoformat()
            await self.repo.add_message(
                session_id=session_id,
                role="assistant",
                content=content,
                timestamp=assistant_timestamp,
                tool_calls=tool_calls if tool_calls else None,
            )

            # ---------- 6. 无工具调用：检查兜底执行 ----------
            if not tool_calls:
                matched_rule = context.get('_matched_rule')
                tool_name_forced = matched_rule.get('tool_name', '') if matched_rule else ''
                tool_obj = self.registry._tools.get(tool_name_forced) if matched_rule else None
                if (
                    matched_rule
                    and not context.get('_tool_executed')
                    and tool_obj
                    and tool_obj.risk_level == 'low'
                    and tool_obj.allow_forced_exec
                ):
                    tool_name = tool_name_forced
                    forced_args = RuleEngineCore.extract_args(matched_rule, text)
                    if forced_args is not None:
                        forced_id = f"forced_{matched_rule.get('rule_id', 'unknown')}_{uuid.uuid4().hex[:8]}"
                        try:
                            result_text, images = await asyncio.wait_for(
                                self.registry.execute_tool(tool_name, forced_args, context),
                                timeout=TOOL_EXEC_TIMEOUT,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(f"[Agent] 兜底工具 '{tool_name}' 超时")
                            result_text = f"工具 '{tool_name}' 执行超时。"
                            images = []
                        except Exception as e:
                            logger.error(f"[Agent] 兜底工具 '{tool_name}' 异常: {e}")
                            result_text = f"工具 '{tool_name}' 执行出错: {e}"
                            images = []

                        all_images.extend(images)
                        context['_tool_executed'] = True

                        # 伪造 assistant 消息（含 tool_calls）
                        forged_assistant = {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": forced_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(forced_args),
                                },
                            }],
                        }
                        messages.append(forged_assistant)

                        # 伪造 tool 结果消息
                        forged_tool = {
                            "role": "tool",
                            "tool_call_id": forced_id,
                            "name": tool_name,
                            "content": result_text,
                        }
                        messages.append(forged_tool)

                        # 持久化到数据库
                        await self.repo.add_message(
                            session_id=session_id,
                            role="assistant",
                            content="",
                            timestamp=datetime.now().isoformat(),
                            tool_calls=forged_assistant["tool_calls"],
                        )
                        await self.repo.add_message(
                            session_id=session_id,
                            role="tool",
                            content=result_text,
                            timestamp=datetime.now().isoformat(),
                            name=tool_name,
                            tool_calls={"tool_call_id": forced_id},
                        )

                        logger.info(f"[Agent] 兜底执行: {tool_name} args={forced_args}")
                        continue  # 让 LLM 基于 tool 结果生成最终回复

                final_text = content
                break

            # ---------- 7. 有工具调用：追加 assistant 到上下文流 ----------
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            # ---------- 8. 执行工具并追加结果 ----------
            task_completed = False
            for tc in tool_calls:
                tool_id = tc.get("id", "")
                func_name = tc.get("function", {}).get("name", "")
                arguments_str = tc.get("function", {}).get("arguments", "{}")
                try:
                    arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
                except json.JSONDecodeError:
                    arguments = {}

                try:
                    result_text, images = await asyncio.wait_for(
                        self.registry.execute_tool(func_name, arguments, context),
                        timeout=TOOL_EXEC_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[Agent] 工具 '{func_name}' 执行超时（{TOOL_EXEC_TIMEOUT}s），返回降级提示")
                    result_text = f"Error: Tool '{func_name}' execution timed out. Please skip this step or ask user for confirmation."
                    images = []
                all_images.extend(images)

                # ── 显式完成信号（需开启动态循环） ──
                if plugin_config.enable_dynamic_loop and func_name == "mark_task_complete":
                    final_text = content or result_text
                    task_completed = True

                tool_timestamp = datetime.now().isoformat()
                await self.repo.add_message(
                    session_id=session_id,
                    role="tool",
                    content=result_text,
                    timestamp=tool_timestamp,
                    name=func_name,
                    tool_calls={"tool_call_id": tool_id}  # <--- 新增这一行，将 ID 存入数据库的 JSON 字段中
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": func_name,
                    "content": result_text,
                })

            if task_completed:
                logger.info(f"[Agent] {session_id} LLM 调用 mark_task_complete，退出循环")
                break

        # 循环结束（正常退出或耗尽）
        if not final_text:
            last_assistant = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
            final_text = last_assistant.get("content", "") if last_assistant else "任务完成。"

        # 命中统计更新
        matched_rule = context.get('_matched_rule')
        if matched_rule and context.get('_tool_executed'):
            try:
                await self.rule_repo.increment_hit_count(matched_rule['rule_id'])
            except Exception as e:
                logger.warning(f"[Agent] 更新命中统计失败: {e}")

        # 触发即遗忘：后台异步记忆压缩（受熔断器保护）
        from .. import circuit_breaker
        if circuit_breaker is None or circuit_breaker.allow_new_task():
            asyncio.create_task(
                self.memory_service.process_session_memory(session_id)
            )
        else:
            logger.debug("[Agent] 记忆压缩熔断中，跳过入队")
        return {"text": final_text, "images": all_images}
