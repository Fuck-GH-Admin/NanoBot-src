# src/plugins/chatbot/matchers/event_notice.py

from datetime import datetime, timedelta
from nonebot import on_notice, on_request
from nonebot.adapters.onebot.v11 import (
    Bot,
    PokeNotifyEvent,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
    FriendRequestEvent,
    FriendAddNoticeEvent,
    MessageSegment,
)
from nonebot.log import logger

from ..config import plugin_config
from ..services import img_srv, agent_srv, perm_srv

# ---------- 戳一戳 ----------
poke = on_notice(priority=5, block=True)
user_state = {}

@poke.handle()
async def handle_poke(bot: Bot, event: PokeNotifyEvent):
    if event.target_id != event.self_id:
        return
    user_id = event.user_id

    with logger.contextualize(group_id=event.group_id or 0):
        now = datetime.now()
        state = user_state.get(user_id, {"count": 0, "last_time": now})
        if now - state["last_time"] > timedelta(seconds=30):
            state = {"count": 0, "last_time": now}
        state["last_time"] = now
        state["count"] += 1

        # —— 连续戳特殊逻辑（保留）——
        # 第 3 次：发图
        if state["count"] == 3:
            path, _ = await img_srv.get_image("", allow_r18=False)
            if path:
                state["count"] = 0
                user_state[user_id] = state
                await poke.finish(MessageSegment.image(f"file:///{path}"))
        # 第 4 次：禁言警告 + 执行
        elif state["count"] == 4 and event.group_id:
            try:
                await bot.set_group_ban(group_id=event.group_id, user_id=user_id, duration=60)
                user_state[user_id] = state
                await poke.finish("哼！让你戳！禁言1分钟！")
            except:
                user_state[user_id] = state
                await poke.finish("呜呜…我没权限禁言你QAQ")
            return
        # 第 5 次：叫主人
        elif state["count"] == 5:
            master = list(plugin_config.superusers)[0] if plugin_config.superusers else "2797364016"
            state["count"] = 0
            user_state[user_id] = state
            await poke.finish(MessageSegment.at(master) + f" 主人救命！{user_id} 老戳我！！")

        # —— LLM 生成自然回应 ——
        try:
            if event.group_id:
                info = await bot.get_group_member_info(group_id=event.group_id, user_id=user_id)
                sender_name = info.get("card") or info.get("nickname", str(user_id))
            else:
                info = await bot.get_stranger_info(user_id=user_id)
                sender_name = info.get("nickname", str(user_id))
        except:
            sender_name = str(user_id)

        action_text = f"[动作] {sender_name} 轻轻戳了戳你"

        context = {
            "bot": bot,
            "user_id": str(user_id),
            "group_id": event.group_id if event.group_id else 0,
            "is_admin": False,
            "sender_name": sender_name,
            "allow_r18": False,
            "source_type": "system",
            "permission_service": perm_srv,
        }
        try:
            result = await agent_srv.run_agent("system_poke", action_text, context)
            reply_text = result.get("text") or "唔…"
        except Exception:
            reply_text = "唔…"

        user_state[user_id] = state
        await poke.finish(reply_text)


# ---------- 进出群欢迎 ----------
# 同一 matcher 下通过事件类型注解自动分发：GroupIncreaseNoticeEvent → handle_increase, GroupDecreaseNoticeEvent → handle_decrease
welcome = on_notice(priority=5, block=False)

async def check_group(group_id: str) -> bool:
    if not plugin_config.welcome_groups:
        return True
    return group_id in plugin_config.welcome_groups

@welcome.handle()
async def handle_increase(bot: Bot, event: GroupIncreaseNoticeEvent):
    if event.user_id == event.self_id: return
    if plugin_config.welcome_mode not in ["hello", "all"]: return
    if not await check_group(str(event.group_id)): return
    with logger.contextualize(group_id=event.group_id):
        try:
            ctx = {"bot": bot, "user_id": str(event.user_id), "group_id": event.group_id, "is_admin": False, "allow_r18": False, "source_type": "system"}
            result = await agent_srv.run_agent("system_welcome",
                "用可爱温暖的语气欢迎一位新朋友加入群聊，30字以内，可以加表情", ctx)
            reply = result.get("text") or "欢迎新朋友加入～"
            await welcome.finish(MessageSegment.at(event.user_id) + f" {reply}")
        except Exception:
            await welcome.finish(MessageSegment.at(event.user_id) + " 欢迎新朋友加入～")

@welcome.handle()
async def handle_decrease(bot: Bot, event: GroupDecreaseNoticeEvent):
    if event.user_id == event.self_id: return
    if plugin_config.welcome_mode not in ["bye", "all"]: return
    if not await check_group(str(event.group_id)): return
    with logger.contextualize(group_id=event.group_id):
        try:
            name = "群友"
            try:
                info = await bot.get_group_member_info(group_id=event.group_id, user_id=event.user_id)
                name = info.get("nickname", "群友")
            except: pass
            ctx = {"bot": bot, "user_id": str(event.user_id), "group_id": event.group_id, "is_admin": False, "allow_r18": False, "source_type": "system"}
            result = await agent_srv.run_agent("system_welcome",
                f"用有点伤感但不过分的语气说再见，提到“{name}”，25字以内", ctx)
            reply = result.get("text") or f"{name} 离开了大家庭...常回来看看哦"
            await welcome.finish(reply)
        except Exception:
            await welcome.finish(f"{name} 离开了大家庭...常回来看看哦")


# ---------- 好友事件：自动加入私聊白名单 ----------

async def _add_to_private_whitelist(user_id: str):
    """将 user_id 原子地追加到 private_whitelist 并落盘。"""
    from ..config import plugin_config as _cfg

    with _cfg._lock:
        payload = {}
        for name, field_info in _cfg._config.__class__.model_fields.items():
            val = getattr(_cfg._config, name)
            if isinstance(val, set):
                val = list(val)
            elif name == "group_configs":
                from ..config import GroupSettings
                val = {k: v.model_dump() if isinstance(v, GroupSettings) else v
                       for k, v in val.items()}
            payload[name] = val

    whitelist = set(payload.get("private_whitelist", []))
    if user_id in whitelist:
        return
    whitelist.add(user_id)
    payload["private_whitelist"] = list(whitelist)
    _cfg.save_config(payload)
    logger.info(f"[EventNotice] 已将用户 {user_id} 加入私聊白名单配置文件")


friend_request = on_request(priority=5, block=True)

@friend_request.handle()
async def handle_friend_request(bot: Bot, event: FriendRequestEvent):
    await event.approve(bot)
    await _add_to_private_whitelist(str(event.user_id))
    await friend_request.finish()


friend_add = on_notice(priority=5, block=False)

@friend_add.handle()
async def handle_friend_add(bot: Bot, event: FriendAddNoticeEvent):
    await _add_to_private_whitelist(str(event.user_id))