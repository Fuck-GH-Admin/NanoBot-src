# src/plugins/chatbot/services/agent_service.py

import asyncio
import json
import httpx
from datetime import datetime
from typing import Dict, List, Any
from nonebot.log import logger

from ..config import plugin_config
from ..schemas import ChatRequestPayload
from ..repositories.memory_repo import MemoryRepository
from ..tools.registry import ToolRegistry
from ..tools.image_tool import GenerateImageTool, SearchAcgImageTool
from ..tools.admin_tool import BanUserTool
from ..tools.book_tool import RecommendBookTool, JmDownloadTool
from ..tools.system_tool import MarkTaskCompleteTool
from .memory_service import MemoryService
from ..utils.embedding import create_semantic_lorebook

# 发给 Node.js 的近期消息轮次上限
RECENT_MESSAGES_LIMIT = 30

# 重复检测相似度阈值
JACCARD_THRESHOLD = 0.9


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
        self.node_url = plugin_config.node_chat_url
        self.registry = ToolRegistry()
        self.http_client = httpx.AsyncClient(timeout=plugin_config.agent_request_timeout)
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

        user_msg_id = await self.repo.add_message(
            session_id=session_id,
            role="user",
            content=text,
            user_id=user_id,
            name=sender_name,
            timestamp=now_iso,
        )

        # ---------- 3. 精准备料：从数据库读取近期上下文 ----------
        # 3a. 最近 N 条消息（作为发给 Node.js 的 chatHistory）
        recent_msgs = await self.repo.get_recent_messages(
            session_id, limit=RECENT_MESSAGES_LIMIT
        )
        messages = [self._to_openai_message(m) for m in recent_msgs]

        # 3b. 提取活跃 user_id 集合，精准获取画像
        active_uids = list({
            m.get("user_id") for m in recent_msgs if m.get("user_id")
        })
        profiles_raw = await self.repo.get_active_profiles(session_id, active_uids)

        # 3c. 读取群组宏观摘要
        existing_summary = await self.repo.get_group_summary(session_id)

        # 3d. 查询活跃实体间的关系图（带衰减）
        decayed_relations = []
        if plugin_config.entity_relation_enabled:
            entity_ids = [f"user_{uid}" for uid in active_uids if uid]
            decayed_relations = await self.repo.get_relations_with_decay(
                session_id, entity_ids=entity_ids if entity_ids else None
            )

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

            # 语义向量检索（降级安全：失败返回空列表）
            if plugin_config.semantic_lorebook_enabled and self.semantic_lorebook and text:
                try:
                    semantic_hits = await self.semantic_lorebook.search(text, top_k=3)
                    lorebook_context["semantic_hits"] = semantic_hits
                except Exception as e:
                    logger.warning(f"[Agent] 语义检索失败，降级为纯关键词: {e}")

            # 构建 memorySnapshot（符合 ChatRequestPayload schema）
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

            # Pydantic 校验：确保发出的 payload 格式正确
            validated = ChatRequestPayload(
                chatHistory=messages,
                memorySnapshot=memory_snapshot,
                tools=tools,
                context=lorebook_context,
            )
            payload = validated.model_dump()

            try:
                resp = await self.http_client.post(self.node_url, json=payload)
                if resp.status_code != 200:
                    logger.error(f"[Agent] Node API error {resp.status_code}: {resp.text}")
                    return {"text": "大脑短路了，等一下再试吧...", "images": all_images}
                data = resp.json()
            except Exception as e:
                logger.error(f"[Agent] 请求 Node 失败: {e}")
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

                result_text, images = await self.registry.execute_tool(func_name, arguments, context)
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

        # 触发即遗忘：后台异步记忆压缩
        asyncio.create_task(
            self.memory_service.process_session_memory(session_id)
        )
        return {"text": final_text, "images": all_images}
