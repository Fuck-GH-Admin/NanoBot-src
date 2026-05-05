# src/plugins/chatbot/services/agent_service.py

import asyncio
import json
import httpx
from datetime import datetime
from typing import Dict, Any
from nonebot.log import logger

from ..config import plugin_config
from .prompt_adapter import PromptAdapter
from ..repositories.memory_repo import MemoryRepository
from ..tools.registry import ToolRegistry
from ..tools.image_tool import GenerateImageTool, SearchAcgImageTool
from ..tools.admin_tool import BanUserTool
from ..tools.book_tool import RecommendBookTool, JmDownloadTool
from ..tools.system_tool import MarkTaskCompleteTool
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
        self._register_tools()

    def _register_tools(self):
        self.registry.register(GenerateImageTool())
        self.registry.register(SearchAcgImageTool())
        self.registry.register(BanUserTool())
        self.registry.register(RecommendBookTool())
        self.registry.register(JmDownloadTool())
        if plugin_config.enable_dynamic_loop:
            self.registry.register(MarkTaskCompleteTool())

    async def close(self):
        await self.http_client.aclose()

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
        if msg.get("tool_calls"):
            result["tool_calls"] = msg["tool_calls"]
        return result

    async def run_agent(
        self,
        user_id: str,
        text: str,
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        :param context: 必须包含:
            - permission_service
            - is_admin: bool
            - group_id: int (私聊为0)
            - allow_r18: bool
            - bot: Bot 实例
            - sender_name: str (发送者昵称)
            - drawing_service (可选)
            - image_service (可选)
            - book_service (可选)
        """
        # ---------- 1. 会话身份 ----------
        group_id = context.get("group_id", 0)
        session_id = self._build_session_id(group_id, user_id)

        # ---------- 2. 第一时间落库：记录 User 消息 ----------
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
            compiled_messages = self.prompt_adapter.compile_prompt(
                chat_history=messages,
                snapshot=memory_snapshot,
                context=lorebook_context,
            )

            # 直连 LLM API
            api_payload: Dict[str, Any] = {
                "model": plugin_config.deepseek_model_name,
                "messages": compiled_messages,
                "temperature": 0.7,
                "max_tokens": 2048,
            }
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
                    return {"text": "大脑短路了，等一下再试吧...", "images": all_images}
                data = resp.json()
                reset_cooldown()
            except Exception as e:
                logger.error(f"[Agent] 请求 LLM 失败: {e}")
                return {"text": "连接至思考中心失败，请稍后重试。", "images": all_images}

            choices = data.get("choices", [])
            if not choices:
                return {"text": "我没想好怎么回答...", "images": all_images}

            msg = choices[0].get("message", {})
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

            # ---------- 6. 无工具调用：正常结束 ----------
            if not tool_calls:
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

        # 触发即遗忘：后台异步记忆压缩（受熔断器保护）
        from .. import circuit_breaker
        if circuit_breaker is None or circuit_breaker.allow_new_task():
            asyncio.create_task(
                self.memory_service.process_session_memory(session_id)
            )
        else:
            logger.debug("[Agent] 记忆压缩熔断中，跳过入队")
        return {"text": final_text, "images": all_images}
