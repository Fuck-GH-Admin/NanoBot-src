# src/plugins/chatbot/matchers/chat_entry.py

import asyncio
import re
import time
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Union

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent, MessageSegment
from nonebot.log import logger

from ..config import plugin_config, GroupSettings
from ..services import agent_srv, img_srv, draw_srv, book_srv, perm_srv
from ..services.topic_router import topic_router, is_hot_conversation, record_social_event, extract_routing_features


# ─────────────────── Soft Suspend 有限状态机 ───────────────────

@dataclass
class SessionState:
    """单个用户在单个话题中的沉浸会话状态。"""
    topic_id: str
    status: str = "ACTIVE"           # "ACTIVE" | "SOFT_SUSPEND" | "INACTIVE"
    suspend_timestamp: float = 0.0   # 进入 SOFT_SUSPEND 的时间戳
    last_active: float = field(default_factory=time.time)


# 三层嵌套：{group_id: {topic_id: {user_id: SessionState}}}
ACTIVE_SESSIONS: dict[int, dict[str, dict[str, SessionState]]] = {}

# SOFT_SUSPEND 超时（秒）：超过此时间后确认退出
SOFT_SUSPEND_TIMEOUT = 300  # 5 分钟

BOT_WAKE_WORDS = ["elena", "Elena", "艾蕾娜"]

# 噪音消息过滤：纯标点 / 纯表情 / 极短文本（O(1) 复杂度）
_NOISE_PATTERN = re.compile(
    r'^[\s\U00010000-\U0010ffff'
    r' -ᕕ -⁯⸀-⹿'
    r'　-〿＀-￯'
    r'!?!。，、；：""''…—\-_.…|/\\()（）\[\]【】{}<>《》@#$%^&*+=~`\'"]+$'
)
_MIN_SUBSTANTIVE_LEN = 2  # 去除标点后的最小有效字符数

chat_entry = on_message(priority=10, block=False)


async def _cleanup_sessions():
    """每 10 分钟清理过期的沉浸会话条目，精确到 topic_id 粒度。"""
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for gid in list(ACTIVE_SESSIONS.keys()):
            topics = ACTIVE_SESSIONS[gid]
            for tid in list(topics.keys()):
                users = topics[tid]
                # 移除 ACTIVE 超时 + SOFT_SUSPEND 超时的条目
                expired_uids = [
                    uid for uid, st in users.items()
                    if (
                        (st.status == "ACTIVE" and now - st.last_active >= plugin_config.session_timeout)
                        or (st.status == "SOFT_SUSPEND" and now - st.suspend_timestamp >= SOFT_SUSPEND_TIMEOUT)
                    )
                ]
                for uid in expired_uids:
                    del users[uid]
                if not users:
                    del topics[tid]
            if not topics:
                del ACTIVE_SESSIONS[gid]


def _is_noise_message(text: str) -> bool:
    """
    O(1) 短路判定：消息是否为无意义噪音（纯标点、纯表情、极短文本）。
    """
    stripped = _NOISE_PATTERN.sub('', text).strip()
    return len(stripped) < _MIN_SUBSTANTIVE_LEN


def _has_explicit_transfer(event: GroupMessageEvent, bot_self_id: str) -> str | None:
    """
    注意力转移检测：用户是否显式将注意力转向其他人。
    返回被转移目标的 user_id（str），无转移则返回 None。
    """
    # 检测1：引用回复了其他人
    if event.reply and str(event.reply.sender.user_id) != bot_self_id:
        return str(event.reply.sender.user_id)
    # 检测2：@ 了其他人
    for seg in event.message:
        if seg.type == "at":
            qq = seg.data.get("qq")
            if qq != "all" and str(qq) != bot_self_id:
                return str(qq)
    return None


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

    text = " ".join(text_parts).strip()
    # =================================================

    if is_group and not has_substance:
        await chat_entry.finish()

    if is_private and not text:
        await chat_entry.finish("嗯？")

    user_id = str(event.user_id)
    group_id = event.group_id if is_group else 0
    sender_name = await _get_nickname(bot, event)

    # 绑定日志上下文：群聊按 group_id，私聊按 user_id，动态路由到分文件
    # 使用 contextualize() 而非 bind()：通过 contextvars 自动传播到整个异步调用链，
    # 使 agent_service、topic_router、tools、repositories 等下游模块的 logger 调用
    # 自动携带 group_id / private_user_id，由 loguru filter 路由到正确的分文件 sink。
    log_ctx = {"group_id": group_id} if is_group else {"private_user_id": user_id}

    with logger.contextualize(**log_ctx):
        logger.info(f"[ATTENTION] handle_chat 入口: group_id={group_id}, user_id={user_id}, text={text[:50]!r}")

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

        # ---------- 阶段 A：读取群配置 ----------
        if is_group:
            group_cfg = plugin_config.group_configs[str(group_id)]
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
            if not perm_srv.is_private_whitelisted(str(user_id)):
                logger.info(f"[ChatEntry] 非白名单用户 {user_id} 试图私聊，已拦截")
                await chat_entry.finish()
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

            # 4. 沉浸会话窗口（三层嵌套：group → topic → user → SessionState）
            current_time = time.time()
            group_topics = ACTIVE_SESSIONS.get(group_id, {})

            # 遍历所有 topic 寻找该用户的活跃/挂起状态
            found_state: SessionState | None = None
            found_topic_id: str | None = None
            for tid, users in group_topics.items():
                if user_id in users:
                    st = users[user_id]
                    if st.status in ("ACTIVE", "SOFT_SUSPEND"):
                        found_state = st
                        found_topic_id = tid
                        break

            if found_state:
                if is_tome or is_reply_bot or has_wake_word:
                    # 显式触发：不受沉浸窗口限制，直接激活
                    in_active_session = True
                    found_state.status = "ACTIVE"
                    found_state.last_active = current_time
                elif found_state.status == "SOFT_SUSPEND":
                    # SOFT_SUSPEND → ACTIVE：用户再次发言即唤醒
                    # 检测通用扮演称谓用于精准唤醒日志
                    _, generic_terms = extract_routing_features(text)
                    found_state.status = "ACTIVE"
                    found_state.last_active = current_time
                    found_state.suspend_timestamp = 0.0
                    in_active_session = True
                    wake_reason = f"通用称谓唤醒 ({generic_terms})" if generic_terms else "发言唤醒"
                    logger.info(f"[ChatEntry] 用户 {user_id} 从 SOFT_SUSPEND 唤醒 ({wake_reason}, topic={found_topic_id})")
                else:
                    # ACTIVE 状态下的常规判定
                    # 4a. 注意力转移软挂起：@ 其他人或引用其他人 → SOFT_SUSPEND（不销毁）
                    transfer_target = _has_explicit_transfer(event, str(bot.self_id))
                    if transfer_target:
                        found_state.status = "SOFT_SUSPEND"
                        found_state.suspend_timestamp = current_time
                        logger.debug(f"[ChatEntry] 用户 {user_id} 注意力转移至 {transfer_target}，SOFT_SUSPEND (topic={found_topic_id})")
                    # 4b. 噪音过滤：纯标点/表情/极短文本 → 不触发
                    elif _is_noise_message(text):
                        logger.debug(f"[ChatEntry] 沉浸窗口内噪音消息已过滤: {text[:30]}")
                    else:
                        # 4c. 通过所有物理过滤 → 交由 AI 语义判断（在 agent_service 中处理）
                        in_active_session = True

            # 4d. 记录社交事件（无论是否在沉浸窗口内）
            record_social_event(group_id, user_id)

            # 5. 随机插嘴（硬闸前置：高频热聊时直接拦截）
            prob = group_cfg.random_reply_prob
            if prob > 0:
                if is_hot_conversation(group_id):
                    logger.info("[ATTENTION] 触发社交熵抑制，硬闸拦截插话")
                    is_random_hit = False
                else:
                    is_random_hit = random.random() < prob
            else:
                is_random_hit = False

            logger.info(
                f"[ATTENTION] 触发条件判定: is_tome={is_tome}, is_reply_bot={is_reply_bot}, "
                f"has_wake_word={has_wake_word}, in_active_session={in_active_session}, "
                f"is_random_hit={is_random_hit} (prob={prob})"
            )

            if is_tome or is_reply_bot or has_wake_word or in_active_session or is_random_hit:
                should_reply = True

        if not should_reply:
            await chat_entry.finish()

        # ---------- 阶段 B.6：话题路由（必须在 B.5 之前，因为 B.5 需要 topic_id） ----------
        topic_id = None
        if is_group:
            session_id = agent_srv._build_session_id(group_id, user_id)

            # L1 物理强连通：反查被引用消息的 topic_id
            reply_topic_id = None
            if event.reply:
                source_msg_id = str(event.reply.message_id)
                try:
                    reply_topic_id = await asyncio.wait_for(
                        agent_srv.repo.get_topic_id_by_message_id(source_msg_id),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("[ChatEntry] 反查 topic_id 超时，降级走向量路由")

            try:
                topic_id, is_new = await asyncio.wait_for(
                    topic_router.resolve_topic(
                        session_id=session_id,
                        text=text,
                        user_id=user_id,
                        reply_msg_topic_id=reply_topic_id,
                        group_id=group_id,
                        is_explicit_trigger=(is_tome or is_reply_bot or has_wake_word),
                    ),
                    timeout=2.0,
                )
                logger.info(
                    f"[TopicRouter] 路由结果: session_id={session_id}, topic_id={topic_id}, is_new_topic={is_new}"
                )
                if is_new:
                    # 持久化新话题到数据库
                    await agent_srv.repo.upsert_topic_thread(
                        topic_id=topic_id,
                        session_id=session_id,
                        participants=[user_id],
                    )
                else:
                    # 刷新活跃时间和参与者
                    await agent_srv.repo.upsert_topic_thread(
                        topic_id=topic_id,
                        session_id=session_id,
                        participants=[user_id],
                    )
            except asyncio.TimeoutError:
                # 路由超时降级：挂靠最近话题或新建
                logger.warning("[ChatEntry] 话题路由超时，降级处理")
                pool = topic_router.ACTIVE_TOPICS_POOL.get(session_id, [])
                if pool:
                    pool.sort(key=lambda x: x.last_active, reverse=True)
                    topic_id = pool[0].topic_id
                else:
                    topic_id = uuid.uuid4().hex

        # ---------- 阶段 B.7：更新沉浸会话状态（topic 粒度） ----------
        if is_group and topic_id and (is_tome or is_reply_bot or has_wake_word or in_active_session):
            if group_id not in ACTIVE_SESSIONS:
                ACTIVE_SESSIONS[group_id] = {}
            if topic_id not in ACTIVE_SESSIONS[group_id]:
                ACTIVE_SESSIONS[group_id][topic_id] = {}
            now_ts = time.time()
            existing = ACTIVE_SESSIONS[group_id][topic_id].get(user_id)
            if existing:
                existing.status = "ACTIVE"
                existing.last_active = now_ts
                existing.suspend_timestamp = 0.0
            else:
                ACTIVE_SESSIONS[group_id][topic_id][user_id] = SessionState(
                    topic_id=topic_id,
                    status="ACTIVE",
                    last_active=now_ts,
                )
            topic_user_count = len(ACTIVE_SESSIONS[group_id][topic_id])
            logger.info(
                f"[ATTENTION] 沉浸会话已更新: group_id={group_id}, topic_id={topic_id}, "
                f"user_id={user_id}, 该话题活跃用户数={topic_user_count}"
            )

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
            "message_fingerprint": str(getattr(event, "message_id", "")) or None,
            "_active_sessions": ACTIVE_SESSIONS,
            "is_tome": is_tome,
            "is_reply_bot": is_reply_bot,
            "has_wake_word": has_wake_word,
            "topic_id": topic_id,
        }

        result = await agent_srv.run_agent(user_id, text, context)
        reply_text = result.get("text") or ""
        files = result.get("images") or []

        if reply_text:
            await chat_entry.send(reply_text)

        # ---------- 文件载荷分发：按后缀嗅探路由 ----------
        _IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

        for file_path in files:
            try:
                suffix = Path(file_path).suffix.lower()
                if suffix in _IMAGE_SUFFIXES:
                    # 图像流：通过 OneBot 图片段发送
                    await chat_entry.send(MessageSegment.image(f"file:///{file_path}"))
                else:
                    # 实体文件：通过 OneBot 文件上传 API 发送
                    file_name = Path(file_path).name
                    if is_group:
                        await bot.upload_group_file(
                            group_id=group_id,
                            file=str(file_path),
                            name=file_name,
                        )
                    else:
                        await bot.upload_private_file(
                            user_id=int(user_id),
                            file=str(file_path),
                            name=file_name,
                        )
            except Exception as e:
                logger.error(f"发送文件 {file_path} 失败: {e}")

        await chat_entry.finish()
