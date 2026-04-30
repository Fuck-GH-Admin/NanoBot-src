# src/plugins/chatbot/matchers/admin_hard.py

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.params import EventPlainText
from nonebot.log import logger

from ..services import perm_srv

admin_hard = on_message(priority=3, block=False)


@admin_hard.handle()
async def handle_hard_admin(bot: Bot, event: GroupMessageEvent, text: str = EventPlainText()):
    text = text.strip()
    user_id = str(event.user_id)

    # 必须先检查基础权限
    try:
        member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=event.user_id)
        role = member_info.get("role", "member")
    except:
        role = "member"

    if not perm_srv.has_command_privilege(user_id, role):
        return  # block=False，事件继续向下传递

    # 退群指令
    if text == "退群" or text == "leave":
        await admin_hard.send("收到指令，正在退出群聊...")
        await bot.set_group_leave(group_id=event.group_id)
        await admin_hard.finish()

    # 可以继续添加其他硬控，如重载配置、清空记忆等
