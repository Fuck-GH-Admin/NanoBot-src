# src/plugins/chatbot/matchers/event_notice.py

import random
from datetime import datetime, timedelta
from nonebot import on_notice
from nonebot.adapters.onebot.v11 import Bot, PokeNotifyEvent, GroupIncreaseNoticeEvent, GroupDecreaseNoticeEvent, MessageSegment
from nonebot.log import logger

from ..config import plugin_config
from ..services import img_srv, agent_srv

# 戳一戳随机回复语料池
POKE_REPLIES = [
    "戳我干嘛？",
    "别戳啦！痒死了QAQ",
    "戳我也没用，我不会给你发图的！",  # 触发发图
    "你再戳我就把你禁言一分钟哦～",    # 触发警告
    "好痒！停下！我要叫主人了！"       # 触发叫主人
]

# ---------- 戳一戳 ----------
poke = on_notice(priority=5, block=True)
user_state = {}

@poke.handle()
async def handle_poke(bot: Bot, event: PokeNotifyEvent):
    if event.target_id != event.self_id:
        return
    user_id = event.user_id
    now = datetime.now()
    state = user_state.get(user_id, {"count": 0, "last_time": now, "last_reply": None})
    if now - state["last_time"] > timedelta(seconds=30):
        state = {"count": 0, "last_time": now, "last_reply": None}
    state["last_time"] = now
    state["count"] += 1

    # 特殊连续行为逻辑（发图、禁言、叫主人）
    last = state.get("last_reply")
    if last == "戳我也没用，我不会给你发图的！":
        path, _ = await img_srv.get_image("", allow_r18=False)
        if path:
            await poke.finish(MessageSegment.image(f"file:///{path}"))
    elif last == "你再戳我就把你禁言一分钟哦～":
        if state["count"] >= 3 and event.group_id:
            try:
                await bot.set_group_ban(group_id=event.group_id, user_id=user_id, duration=60)
                await poke.finish("哼！让你戳！禁言1分钟！")
            except:
                await poke.finish("呜呜…我没权限禁言你QAQ")
            finally:
                user_state[user_id] = state
            return
    elif last == "好痒！停下！我要叫主人了！":
        master = list(plugin_config.superusers)[0] if plugin_config.superusers else "2797364016"
        await poke.finish(MessageSegment.at(master) + f" 主人救命！{user_id} 老戳我！！")

    reply = random.choice(POKE_REPLIES)
    if reply in POKE_REPLIES:
        state["last_reply"] = reply
    else:
        state["last_reply"] = None
    user_state[user_id] = state
    await poke.finish(reply)


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
    try:
        ctx = {"bot": bot, "user_id": str(event.user_id), "group_id": event.group_id, "is_admin": False, "allow_r18": False}
        result = await agent_srv.run_agent("system_welcome",
            "用可爱温暖的语气欢迎一位新朋友加入群聊，30字以内，可以加表情", ctx)
        reply = result.get("text", "欢迎新朋友加入～")
        await welcome.finish(MessageSegment.at(event.user_id) + f" {reply}")
    except Exception:
        await welcome.finish(MessageSegment.at(event.user_id) + " 欢迎新朋友加入～")

@welcome.handle()
async def handle_decrease(bot: Bot, event: GroupDecreaseNoticeEvent):
    if event.user_id == event.self_id: return
    if plugin_config.welcome_mode not in ["bye", "all"]: return
    if not await check_group(str(event.group_id)): return
    try:
        name = "群友"
        try:
            info = await bot.get_group_member_info(group_id=event.group_id, user_id=event.user_id)
            name = info.get("nickname", "群友")
        except: pass
        ctx = {"bot": bot, "user_id": str(event.user_id), "group_id": event.group_id, "is_admin": False, "allow_r18": False}
        result = await agent_srv.run_agent("system_welcome",
            f"用有点伤感但不过分的语气说再见，提到“{name}”，25字以内", ctx)
        reply = result.get("text", f"{name} 离开了大家庭...常回来看看哦")
        await welcome.finish(reply)
    except Exception:
        await welcome.finish(f"{name} 离开了大家庭...常回来看看哦")