# src/plugins/chatbot/matchers/chat_entry.py

import random
from datetime import datetime
from typing import Union

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent, MessageSegment
from nonebot.params import EventPlainText
from nonebot.log import logger

from ..config import plugin_config, GroupSettings
from ..services import agent_srv, img_srv, draw_srv, book_srv, perm_srv
from ..repositories.memory_repo import MemoryRepository

memory_repo = MemoryRepository()

chat_entry = on_message(priority=10, block=False)


async def _get_nickname(bot: Bot, event: Union[GroupMessageEvent, PrivateMessageEvent]) -> str:
    """获取发送者的群昵称或 QQ 昵称"""
    user_id = event.user_id
    if isinstance(event, GroupMessageEvent):
        try:
            info = await bot.get_group_member_info(group_id=event.group_id, user_id=user_id)
            return info.get("card") or info.get("nickname", str(user_id))
        except:
            return str(user_id)
    else:
        try:
            info = await bot.get_stranger_info(user_id=user_id)
            return info.get("nickname", str(user_id))
        except:
            return str(user_id)


@chat_entry.handle()
async def handle_chat(bot: Bot, event: Union[GroupMessageEvent, PrivateMessageEvent], text: str = EventPlainText()):
    is_group = isinstance(event, GroupMessageEvent)
    is_private = isinstance(event, PrivateMessageEvent)

    if not is_group and not is_private:
        return

    text = text.strip()
    if not text:
        if is_group:
            # 群聊遇到纯表情/空文本直接忽略
            await chat_entry.finish()
        else:
            await chat_entry.finish("嗯？")
        return

    user_id = str(event.user_id)
    group_id = event.group_id if is_group else 0
    sender_name = await _get_nickname(bot, event)

    # ---------- 新群拦截：未注册群需 @ 机器人且由管理员激活 ----------
    if is_group:
        gid_str = str(group_id)
        if gid_str not in plugin_config.group_configs:
            is_mentioned = False
            try:
                is_mentioned = event.is_tome()
            except:
                pass
            is_authorized = perm_srv.is_superuser(user_id) or perm_srv.is_ai_admin(user_id)
            if not (is_mentioned and is_authorized):
                await chat_entry.finish()
            plugin_config.group_configs[gid_str] = GroupSettings()
            plugin_config.save_config()
            logger.info(f"[ChatEntry] 新群 {group_id} 已激活并加入配置")

    # ---------- 阶段 A：静默记录 (仅群聊) ----------
    if is_group:
        group_cfg = plugin_config.group_configs[str(group_id)]
        if group_cfg.record_all_messages:
            session_id = f"group_{group_id}"
            try:
                await memory_repo.add_message(
                    session_id=session_id,
                    role="user",
                    content=text,
                    user_id=user_id,
                    name=sender_name,
                )
                logger.debug(f"[ChatEntry] 静默记录群 {group_id} 消息: user={user_id}")
            except Exception as e:
                logger.warning(f"[ChatEntry] 静默记录失败: {e}")
    else:
        group_cfg = GroupSettings()

    # ---------- 阶段 B：触发判定 ----------
    should_reply = False

    if is_private:
        should_reply = True
    elif is_group:
        is_tome = False
        try:
            is_tome = event.is_tome()
        except:
            pass

        prob = group_cfg.random_reply_prob
        is_random_hit = random.random() < prob

        if is_tome or is_random_hit:
            should_reply = True

    if not should_reply:
        await chat_entry.finish()

    # ---------- 阶段 C：执行回复 ----------
    is_admin = False
    if is_group:
        try:
            member_info = await bot.get_group_member_info(group_id=group_id, user_id=event.user_id)
            sender_role = member_info.get("role", "member")
            is_admin = perm_srv.has_command_privilege(user_id, sender_role)
        except:
            pass
    else:
        is_admin = perm_srv.is_superuser(user_id)

    context = {
        "bot": bot,
        "user_id": user_id,
        "group_id": group_id,
        "is_admin": is_admin,
        "sender_name": sender_name,
        "allow_r18": group_cfg.allow_r18 if is_group else False,
        "permission_service": perm_srv,
        "drawing_service": draw_srv,
        "image_service": img_srv,
        "book_service": book_srv,
    }

    result = await agent_srv.run_agent(user_id, text, context)
    reply_text = result.get("text", "")
    images = result.get("images", [])

    if reply_text:
        await chat_entry.send(reply_text)

    for img_path in images:
        try:
            await chat_entry.send(MessageSegment.image(f"file:///{img_path}"))
        except Exception as e:
            logger.error(f"发送图片 {img_path} 失败: {e}")

    await chat_entry.finish()
