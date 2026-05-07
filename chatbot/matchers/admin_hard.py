# src/plugins/chatbot/matchers/admin_hard.py

from nonebot_plugin_alconna import on_alconna, Alconna, Args, At
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger

from ..config import plugin_config, GroupSettings
from ..services import perm_srv
from ..schemas import CommandResult


# ---------- 指令定义 ----------

cmd_leave = Alconna("退群")
cmd_activity = Alconna("调整活跃度", Args["prob", str])
cmd_dive = Alconna("潜水模式", Args["action", ["开启", "关闭"]])
cmd_draw_whitelist = Alconna("授权画图", Args["targets", At, ...])
cmd_ban = Alconna("禁言", Args["target", At]["duration", int, 600])


# ---------- Matcher 注册 (block=True 阻断传播到 chat_entry) ----------

admin_leave = on_alconna(cmd_leave, aliases={"leave"}, priority=3, block=True)
admin_activity = on_alconna(cmd_activity, aliases={"活跃度", "插嘴概率"}, priority=3, block=True)
admin_dive = on_alconna(cmd_dive, priority=3, block=True)
admin_draw = on_alconna(cmd_draw_whitelist, aliases={"开启画图白名单"}, priority=3, block=True)
admin_ban = on_alconna(cmd_ban, aliases={"ban"}, priority=3, block=True)


# ---------- 权限检查 ----------

async def _check_privilege(bot: Bot, event: GroupMessageEvent) -> bool:
    user_id = str(event.user_id)
    try:
        member_info = await bot.get_group_member_info(
            group_id=event.group_id, user_id=event.user_id
        )
        role = member_info.get("role", "member")
    except Exception:
        role = "member"
    return perm_srv.has_command_privilege(user_id, role)


# ---------- 退群 ----------

@admin_leave.handle()
async def handle_leave(bot: Bot, event: GroupMessageEvent):
    if not await _check_privilege(bot, event):
        return
    await admin_leave.send("收到指令，正在退出群聊...")
    await bot.set_group_leave(group_id=event.group_id)


# ---------- 活跃度调整 ----------

@admin_activity.handle()
async def handle_activity(bot: Bot, event: GroupMessageEvent, prob: str):
    if not await _check_privilege(bot, event):
        return

    raw = prob.strip()
    try:
        if raw.endswith('%'):
            value = float(raw[:-1]) / 100.0
        else:
            f = float(raw)
            value = f / 100.0 if f > 1.0 else f
        value = max(0.0, min(1.0, value))
    except ValueError:
        await admin_activity.finish("概率格式错误，请输入如 0.5 或 50%")

    gid_str = str(event.group_id)
    gcfg = plugin_config.group_configs.setdefault(gid_str, GroupSettings())
    gcfg.random_reply_prob = value
    try:
        plugin_config.save_config()
        result = CommandResult.ok(
            f"已将本群随机插嘴概率调整为 {value * 100:.1f}%",
            new_prob=value,
        )
        await admin_activity.send(result.message)
    except Exception as e:
        logger.error(f"[Admin] 保存配置失败: {e}")
        result = CommandResult.fail(f"保存配置失败: {e}")
        await admin_activity.send(result.message)


# ---------- 潜水模式切换 ----------

@admin_dive.handle()
async def handle_dive(bot: Bot, event: GroupMessageEvent, action: str):
    if not await _check_privilege(bot, event):
        return

    enabled = action == "开启"
    gid_str = str(event.group_id)
    gcfg = plugin_config.group_configs.setdefault(gid_str, GroupSettings())
    gcfg.record_all_messages = enabled
    try:
        plugin_config.save_config()
        mode_text = "已开启" if enabled else "已关闭"
        result = CommandResult.ok(f"潜水记录模式 {mode_text}", enabled=enabled)
        await admin_dive.send(result.message)
    except Exception as e:
        logger.error(f"[Admin] 保存配置失败: {e}")
        result = CommandResult.fail(f"保存配置失败: {e}")
        await admin_dive.send(result.message)


# ---------- 画图白名单授权 ----------

@admin_draw.handle()
async def handle_draw_whitelist(bot: Bot, event: GroupMessageEvent, targets: list):
    if not await _check_privilege(bot, event):
        return

    if not targets:
        await admin_draw.finish("请 @ 需要授权的用户")

    new_users = set()
    for at_seg in targets:
        qq = str(at_seg.target)
        plugin_config.drawing_whitelist.add(qq)
        new_users.add(qq)

    try:
        plugin_config.save_config()
        names = ", ".join(f"QQ({q})" for q in new_users)
        result = CommandResult.ok(f"已为 {names} 解锁画图模块", users=list(new_users))
        await admin_draw.send(result.message)
    except Exception as e:
        logger.error(f"[Admin] 保存配置失败: {e}")
        result = CommandResult.fail(f"保存配置失败: {e}")
        await admin_draw.send(result.message)


# ---------- 禁言 ----------

@admin_ban.handle()
async def handle_ban(bot: Bot, event: GroupMessageEvent, target: At, duration: int):
    if not await _check_privilege(bot, event):
        return

    from ..tools.system_tools.admin_tool import BanUserTool
    tool = BanUserTool()
    context = {"user_id": str(event.user_id), "group_id": event.group_id}
    result_msg, _ = await tool.execute(
        {"target_id": target.target, "duration": duration, "reason": "管理员控制面指令"},
        context,
    )
    await admin_ban.finish(result_msg)
