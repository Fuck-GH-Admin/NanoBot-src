# src/plugins/chatbot/tools/agent_tools/book_tool.py

from typing import Any, Dict, List, Tuple
from ..base_tool import BaseTool


class RecommendBookTool(BaseTool):
    name = "recommend_book"
    description = '【条件触发】：当用户要求"发点书"、"推荐本子"、"搞点学习资料"时，**绝对禁止假装发送**，必须输出 tool_call 调用此工具，系统会自动发送真实文件。'
    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }
    require_permission = "user"
    risk_level = "low"
    allow_forced_exec = True

    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[str]]:
        # R18 权限阻断
        if not context.get("allow_r18"):
            return "❌ 本群未开启 R18 访问权限，已拦截此操作。", []

        # 获取书籍服务
        book_srv = context.get("book_service")
        if not book_srv:
            from ...services.book_service import BookService
            book_srv = BookService()

        # 获取 Bot 和会话信息
        bot = context.get("bot")
        group_id = context.get("group_id")
        user_id = context.get("user_id")
        is_group = group_id and int(group_id) != 0

        # 获取一本随机书
        path = book_srv.repo.get_random_book()
        if not path:
            return "书架是空的，什么也没有~", []

        # 根据是群聊还是私聊发送文件
        try:
            if is_group:
                await bot.upload_group_file(
                    group_id=int(group_id),
                    file=str(path),
                    name=path.name,
                    timeout=120
                )
            else:
                await bot.upload_private_file(
                    user_id=int(user_id),
                    file=str(path),
                    name=path.name,
                    timeout=120
                )
            return f"📖 偷偷塞给你一本：{path.stem}", []
        except Exception as e:
            return f"发送书籍失败：{e}", []


class JmDownloadTool(BaseTool):
    name = "jm_download"
    is_write_operation = True
    description = '【条件触发】：当用户提供了一串数字ID，并要求"下载"、"看这个本子"时，必须输出 tool_call 调用此工具进行真实下载。'
    parameters = {
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "本子ID列表，如 ['350234', '350235']"
            }
        },
        "required": ["ids"]
    }
    require_permission = "user"
    risk_level = "low"
    allow_forced_exec = True

    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[str]]:
        # R18 权限阻断
        if not context.get("allow_r18"):
            return "❌ 本群未开启 R18 访问权限，已拦截此操作。", []

        ids = arguments.get("ids", [])
        if not ids:
            return "请提供要下载的本子ID", []

        book_srv = context.get("book_service")
        if not book_srv:
            from ...services.book_service import BookService
            book_srv = BookService()

        bot = context.get("bot")
        group_id = context.get("group_id")
        user_id = context.get("user_id")
        is_group = group_id and int(group_id) != 0

        target_id = int(group_id) if is_group else int(user_id)
        message_type = "group" if is_group else "private"

        # 调用书籍服务执行下载与发送
        result_msg = await book_srv.handle_jm_download(bot, target_id, message_type, ids)
        return result_msg, []
