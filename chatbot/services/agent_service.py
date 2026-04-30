# src/plugins/chatbot/services/agent_service.py

import json
import httpx
from datetime import datetime
from typing import Dict, List, Any
from nonebot.log import logger

from ..config import plugin_config
from ..repositories.memory_repo import MemoryRepository
from ..tools.registry import ToolRegistry
from ..tools.image_tool import GenerateImageTool, SearchAcgImageTool
from ..tools.admin_tool import BanUserTool
from ..tools.book_tool import RecommendBookTool, JmDownloadTool


class AgentService:
    def __init__(self):
        self.repo = MemoryRepository()
        self.node_url = plugin_config.node_chat_url
        self.registry = ToolRegistry()
        self._register_tools()

    def _register_tools(self):
        self.registry.register(GenerateImageTool())
        self.registry.register(SearchAcgImageTool())
        self.registry.register(BanUserTool())
        self.registry.register(RecommendBookTool())
        self.registry.register(JmDownloadTool())

    @staticmethod
    def _build_session_id(group_id: int, user_id: str) -> str:
        if group_id and int(group_id) != 0:
            return f"group_{group_id}"
        return f"private_{user_id}"

    @staticmethod
    def _to_openai_message(msg: Dict[str, Any]) -> Dict[str, Any]:
        """
        将内部存储的富信息消息转换为 OpenAI 兼容格式。
        必须透传所有有效身份字段（name, user_id），保证群聊多角色区分。
        """
        result = {
            "role": msg["role"],
            "content": msg.get("content", ""),
        }
        # 透传 name（OpenAI 支持，Node 端依赖此区分群内发言者）
        if "name" in msg:
            result["name"] = msg["name"]
        # 透传 user_id（非必需但未来可扩展）
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
            - sender_name: str (发送者昵称)  **重要：群聊多角色依赖**
            - drawing_service (可选)
            - image_service (可选)
            - book_service (可选)
        """
        # ---------- 1. 会话身份 ----------
        group_id = context.get("group_id", 0)
        session_id = self._build_session_id(group_id, user_id)

        # ---------- 2. 加载记忆（含摘要） ----------
        mem = await self.repo.load_memory(session_id)
        chat_history = mem.get("history", [])          # 保留所有字段（含 name, user_id）
        existing_summary = mem.get("profile", {}).get("summary", "")

        # ---------- 3. 构建当前用户消息（强制保留身份） ----------
        user_msg = {
            "role": "user",
            "user_id": user_id,                       # 必须保留
            "name": context.get("sender_name", "User"), # 必须保留
            "content": text,
            "timestamp": datetime.now().isoformat(),
        }
        chat_history.append(user_msg)
        # 预写防宕机：先写入一次（含完整身份）
        await self.repo.save_memory(session_id, chat_history, mem.get("profile", {}))

        # ---------- 4. 构建发往 Node 的消息列表（透传 name） ----------
        messages = [self._to_openai_message(m) for m in chat_history]

        # 如果有摘要，作为系统消息注入到最前
        if existing_summary.strip():
            messages.insert(0, {"role": "system", "content": f"[Summary of previous events: {existing_summary}]"})

        # 工具 schema
        perm_srv = context.get("permission_service")
        is_admin = context.get("is_admin", False)
        tools = self.registry.get_all_schemas(perm_srv, user_id, is_admin)

        max_loops = plugin_config.agent_max_loops
        all_images = []
        current_summary = existing_summary

        # ---------- 5. ReAct 循环 ----------
        for _ in range(max_loops):
            payload = {
                "chatHistory": messages,
                "tools": tools,
                "user_id": user_id,
            }

            try:
                async with httpx.AsyncClient(timeout=plugin_config.agent_request_timeout) as client:
                    resp = await client.post(self.node_url, json=payload)
                if resp.status_code != 200:
                    logger.error(f"[Agent] Node API error {resp.status_code}: {resp.text}")
                    return {"text": "大脑短路了，等一下再试吧...", "images": []}
                data = resp.json()
            except Exception as e:
                logger.error(f"[Agent] 请求 Node 失败: {e}")
                return {"text": "连接至思考中心失败，请稍后重试。", "images": []}

            choices = data.get("choices", [])
            if not choices:
                return {"text": "我没想好怎么回答...", "images": []}

            msg = choices[0].get("message", {})
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])

            # ---------- 6. 持久化 assistant 消息（无 name，但保留 tool_calls） ----------
            assistant_msg = {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls if tool_calls else None,
                "timestamp": datetime.now().isoformat(),
            }
            chat_history.append(assistant_msg)

            # ---------- 7. 记忆压缩同步：接收并保留 Node.js 返回的富信息历史 ----------
            new_history = data.get("newHistory")
            new_summary = data.get("newSummary")
            if new_history is not None:
                # 用 Node 返回的裁剪/压缩历史替换当前历史（保留所有字段）
                messages = new_history
                # chat_history 也要对应更新，保证后续持久化与 messages 一致
                chat_history = [
                    {k: v for k, v in msg.items() if k not in ("extra", "_extra")}
                    for msg in new_history
                ]
                # 补全可能缺失的时间戳
                for m in chat_history:
                    if "timestamp" not in m:
                        m["timestamp"] = datetime.now().isoformat()
            if new_summary is not None and new_summary.strip():
                current_summary = new_summary

            # ---------- 8. 无工具调用：结束循环 ----------
            if not tool_calls:
                # 将 assistant 消息格式化为 OpenAI 样式并加入 messages
                formatted_assistant = {
                    "role": "assistant",
                    "content": content,
                    "timestamp": assistant_msg["timestamp"],
                }
                messages.append(formatted_assistant)
                # 最终持久化（chat_history 已包含 assistant）
                await self.repo.save_memory(session_id, chat_history, {"summary": current_summary})
                return {"text": content, "images": all_images}

            # ---------- 9. 有工具调用：追加 assistant 到 OpenAI 流 ----------
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            # ---------- 10. 执行工具并追加结果 ----------
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

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": func_name,
                    "content": result_text,
                    "timestamp": datetime.now().isoformat(),
                }
                messages.append(tool_msg)

        # 循环结束（未正常退出）
        last_assistant = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
        final_text = last_assistant.get("content", "") if last_assistant else "任务完成。"
        await self.repo.save_memory(session_id, chat_history, {"summary": current_summary})
        return {"text": final_text, "images": all_images}
