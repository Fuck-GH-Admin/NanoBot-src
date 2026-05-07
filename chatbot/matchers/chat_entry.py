# src/plugins/chatbot/matchers/chat_entry.py

import asyncio
import time
import random
from datetime import datetime
from typing import Union

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent, MessageSegment
from nonebot.log import logger

from ..config import plugin_config, GroupSettings
from ..services import agent_srv, img_srv, draw_srv, book_srv, perm_srv
from ..repositories.memory_repo import MemoryRepository

# 沉浸会话状态管理: {group_id: {user_id: last_active_timestamp}}
ACTIVE_SESSIONS: dict[int, dict[str, float]] = {}
SESSION_TIMEOUT = 120  # 连续对话免 @ 的有效窗口期（秒）
BOT_WAKE_WORDS = ["elena", "Elena", "艾蕾娜"]

memory_repo = MemoryRepository()

chat_entry = on_message(priority=10, block=False)


async def _cleanup_sessions():
    """每 10 分钟清理过期的沉浸会话条目，防止内存泄漏。"""
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for gid in list(ACTIVE_SESSIONS.keys()):
            ACTIVE_SESSIONS[gid] = {
                uid: ts for uid, ts in ACTIVE_SESSIONS[gid].items()
                if now - ts < SESSION_TIMEOUT
            }
            if not ACTIVE_SESSIONS[gid]:
                del ACTIVE_SESSIONS[gid]


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
async def handle_chat(bot: Bot, event: Union[GroupMessageEvent, PrivateMessageEvent]):
    is_group = isinstance(event, GroupMessageEvent)
    is_private = isinstance(event, PrivateMessageEvent)

    if not is_group and not is_private:
        return

    # ========== 消息解析：手动还原 @ 提及 ==========
    text_parts = []
    has_substance = False  # 是否包含非 @ 的实质文本

    for seg in event.message:
        if seg.type == "text":
            t = seg.data.get("text", "").strip()
            if t:
                text_parts.append(t)
                has_substance = True
        elif seg.type == "at":
            qq = seg.data.get("qq")
            name = seg.data.get("name", "")
            if str(qq) == str(bot.self_id):
                text_parts.append("@Bot")
            elif str(qq) == "all":
                text_parts.append("@全体成员")
                has_substance = True
            else:
                if name:
                    text_parts.append(f"@{name}(QQ:{qq})")
                else:
                    text_parts.append(f"@用户_{qq}")
                has_substance = True

    text = " ".join(text_parts).strip()
    # =================================================

    if is_group and not has_substance:
        await chat_entry.finish()

    if is_private and not text:
        await chat_entry.finish("嗯？")

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
    is_tome = False
    is_reply_bot = False
    has_wake_word = False
    in_active_session = False
    is_random_hit = False

    if is_private:
        should_reply = True
    elif is_group:
        # 1. @ 机器人
        try:
            is_tome = event.is_tome()
        except:
            pass

        # 2. 引用回复 Bot 的消息
        if event.reply and str(event.reply.sender.user_id) == str(bot.self_id):
            is_reply_bot = True

        # 3. 唤醒词
        if any(word.lower() in text.lower() for word in BOT_WAKE_WORDS):
            has_wake_word = True

        # 4. 沉浸会话窗口（增加对话转移检测）
        current_time = time.time()
        group_sessions = ACTIVE_SESSIONS.get(group_id, {})
        last_active = group_sessions.get(user_id, 0)

        if current_time - last_active < SESSION_TIMEOUT:
            if is_tome or is_reply_bot or has_wake_word:
                in_active_session = True
            else:
                dialogue_transferred = False

                # 检测1：引用回复了其他人
                if event.reply and str(event.reply.sender.user_id) != str(bot.self_id):
                    dialogue_transferred = True

                # 检测2：@ 了其他人
                if not dialogue_transferred:
                    for seg in event.message:
                        if seg.type == "at":
                            qq = seg.data.get("qq")
                            if qq != "all" and str(qq) != str(bot.self_id):
                                dialogue_transferred = True
                                break

                if not dialogue_transferred:
                    in_active_session = True

        # 5. 随机插嘴
        prob = group_cfg.random_reply_prob
        is_random_hit = random.random() < prob

        if is_tome or is_reply_bot or has_wake_word or in_active_session or is_random_hit:
            should_reply = True

    if not should_reply:
        await chat_entry.finish()

    # ---------- 阶段 B.5：更新沉浸会话状态 ----------
    if is_group and (is_tome or is_reply_bot or has_wake_word or in_active_session):
        if group_id not in ACTIVE_SESSIONS:
            ACTIVE_SESSIONS[group_id] = {}
        ACTIVE_SESSIONS[group_id][user_id] = time.time()

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
        "scope_type": "group" if is_group else "private",
        "scope_id": str(group_id) if is_group else user_id,
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
