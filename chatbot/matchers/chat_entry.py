# src/plugins/chatbot/matchers/chat_entry.py

from typing import Union

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent, MessageSegment
from nonebot.params import EventPlainText
from nonebot.log import logger

from ..services.agent_service import AgentService
from ..services import img_srv, draw_srv, book_srv, perm_srv

agent = AgentService()

# 优先级 10，普通消息
chat_entry = on_message(priority=10, block=False)


@chat_entry.handle()
async def handle_chat(bot: Bot, event: Union[GroupMessageEvent, PrivateMessageEvent], text: str = EventPlainText()):
    # 仅处理 @机器人 的消息（群聊）或私聊
    is_group = isinstance(event, GroupMessageEvent)
    is_private = isinstance(event, PrivateMessageEvent)

    if is_group:
        try:
            if not event.is_tome():
                return
        except Exception:
            return  # 无法判断是否提及 Bot 时，静默跳过
    elif is_private:
        # 私聊直接回复（可加白名单检查，但由 config 控制）
        pass
    else:
        return

    text = text.strip()
    if not text:
        if is_group:
            await chat_entry.finish("🤔 发空气干嘛？")
        else:
            await chat_entry.finish("嗯？")
        return

    user_id = str(event.user_id)
    group_id = event.group_id if is_group else 0

    # 获取权限上下文
    is_admin = False
    if is_group:
        try:
            member_info = await bot.get_group_member_info(group_id=group_id, user_id=event.user_id)
            sender_role = member_info.get("role", "member")
            is_admin = perm_srv.has_command_privilege(user_id, sender_role)
        except:
            pass
    else:
        # 私聊中超级用户自动视为 admin
        is_admin = perm_srv.is_superuser(user_id)

    # 构建上下文
    context = {
        "bot": bot,
        "user_id": user_id,
        "group_id": group_id,
        "is_admin": is_admin,
        "allow_r18": True,  # 群聊暂时允许，可根据配置调整
        "permission_service": perm_srv,
        "drawing_service": draw_srv,
        "image_service": img_srv,
        "book_service": book_srv,
    }

    # 调用 Agent
    result = await agent.run_agent(user_id, text, context)
    reply_text = result.get("text", "")
    images = result.get("images", [])

    # 发送文本
    if reply_text:
        # 如果消息过长或需要分段可扩展
        await chat_entry.send(reply_text)

    # 发送图片
    for img_path in images:
        try:
            await chat_entry.send(MessageSegment.image(f"file:///{img_path}"))
        except Exception as e:
            logger.error(f"发送图片 {img_path} 失败: {e}")

    # 必须 finish 防止后续 matcher 执行
    await chat_entry.finish()
