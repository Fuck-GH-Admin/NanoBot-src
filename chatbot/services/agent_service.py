# src/plugins/chatbot/services/agent_service.py

import json
import httpx
from typing import Dict, List, Any
from nonebot.log import logger

from ..config import plugin_config
from ..repositories.memory_repo import MemoryRepository
from ..tools.registry import ToolRegistry
from ..tools.image_tool import GenerateImageTool, SearchAcgImageTool
from ..tools.admin_tool import BanUserTool
from ..tools.book_tool import RecommendBookTool, JmDownloadTool


class AgentService:
    """
    瘦终端 Agent：只负责与 Node.js 大脑交互，完成 ReAct 循环。
    不进行任何意图分析或 prompt 拼装，所有智慧由远端大模型提供。
    """

    def __init__(self):
        self.repo = MemoryRepository()
        self.node_url = getattr(plugin_config, "node_chat_url", "http://127.0.0.1:3000/api/chat")
        self.registry = ToolRegistry()
        self._register_tools()

    def _register_tools(self):
        # 所有工具在此注册
        self.registry.register(GenerateImageTool())
        self.registry.register(SearchAcgImageTool())
        self.registry.register(BanUserTool())
        self.registry.register(RecommendBookTool())
        self.registry.register(JmDownloadTool())

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
            - drawing_service (可选)
            - image_service (可选)
            - book_service (可选)
        :return: {"text": "最终回复", "images": ["/path/...", ...]}
        """
        # 1. 加载历史记忆
        mem = await self.repo.load_memory(user_id)
        chat_history = mem.get("history", [])
        # 不再使用 profile，全部交给 Node 处理

        # 2. 获取当前可用的工具 schema
        perm_srv = context.get("permission_service")
        is_admin = context.get("is_admin", False)
        tools = self.registry.get_all_schemas(perm_srv, user_id, is_admin)

        # 3. 构建初始请求
        messages = []
        if chat_history:
            messages.extend(chat_history[-20:])  # 只保留最近 20 轮
        messages.append({"role": "user", "content": text})

        max_loops = 5
        all_images = []

        for _ in range(max_loops):
            payload = {
                "chatHistory": messages,
                "tools": tools,
                "user_id": user_id
            }

            try:
                async with httpx.AsyncClient(timeout=60) as client:
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

            # 如果没有工具调用，结束循环
            if not tool_calls:
                # 将助手消息加入历史（可选，这里不持久化，但发送给 Node 的历史应包含）
                messages.append({"role": "assistant", "content": content})
                return {"text": content, "images": all_images}

            # 有工具调用，将助手消息（包含 tool_calls）加入历史
            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

            # 执行每一个工具调用
            for tc in tool_calls:
                tool_id = tc.get("id", "")
                func_name = tc.get("function", {}).get("name", "")
                arguments_str = tc.get("function", {}).get("arguments", "{}")
                try:
                    arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
                except json.JSONDecodeError:
                    arguments = {}

                # 交给注册表执行，会自动做权限拦截和错误处理
                result_text, images = await self.registry.execute_tool(func_name, arguments, context)
                all_images.extend(images)

                # 将工具结果追加到历史
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": func_name,
                    "content": result_text
                })

        # 如果循环结束仍未返回纯文本，尝试提取最后一条助手消息
        last_assistant = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
        final_text = last_assistant.get("content", "") if last_assistant else "任务完成。"
        return {"text": final_text, "images": all_images}