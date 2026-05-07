# src/plugins/chatbot/tools/system_tools/admin_tool.py

from typing import Any, Dict, List, Tuple
from ..base_tool import BaseTool


class BanUserTool(BaseTool):
    name = "ban_user"
    is_write_operation = True
    description = '【条件触发】：当管理员命令你"禁言XXX"、"让他闭嘴"时，必须输出 tool_call 调用此工具来执行真实的群管操作，绝对禁止仅用文字恐吓。'
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
    risk_level = "high"
    allow_forced_exec = False

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
            from ...services.permission_service import PermissionService
            permission_srv = PermissionService()

        result_msg = await permission_srv.ban_user(
            bot,
            group_id,
            int(target_id),
            duration,
            operator_id=context["user_id"],
            reason=reason
        )

        from ...services.shadow_context import ShadowContext
        ShadowContext().push(
            f"group_{group_id}",
            f"管理员 {context['user_id']} 禁言了用户 {target_id}，时长 {duration} 秒"
        )

        import time
        from ...repositories.memory_repo import MemoryRepo
        await MemoryRepo().insert_tool_log(
            session_id=f"group_{group_id}",
            request_id=f"ban_{target_id}_{int(time.time())}",
            step=1,
            trigger="control_plane",
            tool_name="ban_user",
            arguments={"target_id": target_id, "duration": duration, "reason": reason},
            result_summary=result_msg[:300] if result_msg else "",
        )

        return result_msg, []
