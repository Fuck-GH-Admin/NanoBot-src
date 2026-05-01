# src/plugins/chatbot/matchers/chat_entry.py

import random
from datetime import datetime
from typing import Union

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent, MessageSegment
from nonebot.params import EventPlainText
from nonebot.log import logger

from ..config import plugin_config, GroupSettings
from ..services.agent_service import AgentService
from ..services import img_srv, draw_srv, book_srv, perm_srv
from ..repositories.memory_repo import MemoryRepository

agent = AgentService()
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

    # ---------- 阶段 A：静默记录 (仅群聊) ----------
    if is_group:
        group_cfg = plugin_config.group_configs.get(str(group_id), GroupSettings())
        if group_cfg.record_all_messages:
            session_id = f"group_{group_id}"
            user_msg = {
                "role": "user",
                "user_id": user_id,
                "name": sender_name,
                "content": text,
                "timestamp": datetime.now().isoformat(),
            }
            try:
                mem = await memory_repo.load_memory(session_id)
                history = mem.get("history", [])
                history.append(user_msg)
                await memory_repo.save_memory(session_id, history, mem.get("profile", {}))
                logger.debug(f"[ChatEntry] 静默记录群 {group_id} 消息: {user_msg}")
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
        "allow_r18": True,
        "permission_service": perm_srv,
        "drawing_service": draw_srv,
        "image_service": img_srv,
        "book_service": book_srv,
    }

    result = await agent.run_agent(user_id, text, context)
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
