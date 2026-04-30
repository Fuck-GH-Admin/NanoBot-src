# src/plugins/chatbot/tools/admin_tool.py

from typing import Any, Dict, List, Tuple
from .base_tool import BaseTool


class BanUserTool(BaseTool):
    name = "ban_user"
    description = "禁言指定的群成员。需要管理员权限。"
    parameters = {
        "type": "object",
        "properties": {
            "target_id": {
                "type": "integer",
                "description": "要禁言的成员 QQ 号"
            },
            "duration": {
                "type": "integer",
                "description": "禁言时长，单位秒。0 表示解除禁言"
            },
            "reason": {
                "type": "string",
                "description": "禁言原因（可选）"
            }
        },
        "required": ["target_id", "duration"]
    }
    require_permission = "admin"

    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[str]]:
        target_id = arguments.get("target_id")
        duration = arguments.get("duration", 1800)
        reason = arguments.get("reason", "AI 指令")

        if not target_id:
            return "错误：未指定目标 QQ 号", []

        bot = context.get("bot")
        group_id = context.get("group_id")
        if not bot or not group_id:
            return "错误：缺少 Bot 实例或群号，无法执行管理操作。", []

        permission_srv = context.get("permission_service")
        if not permission_srv:
            from ..services.permission_service import PermissionService
            permission_srv = PermissionService()

        result_msg = await permission_srv.ban_user(
            bot,
            group_id,
            int(target_id),
            duration,
            operator_id=context["user_id"],
            reason=reason
        )
        return result_msg, []