# src/plugins/chatbot/matchers/admin_hard.py

import re
import random
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import EventPlainText
from nonebot.log import logger

from ..config import plugin_config, GroupSettings
from ..services import perm_srv

admin_hard = on_message(priority=3, block=False)


def parse_prob(text: str):
    """从指令中解析插嘴概率（支持百分比或小数）"""
    match = re.search(r'(?:调整活跃度|插嘴概率)\s*(\d+(\.\d+)?%?)', text)
    if not match:
        return None
    value = match.group(1).strip()
    try:
        if value.endswith('%'):
            prob = float(value[:-1]) / 100.0
        else:
            prob = float(value)
        return max(0.0, min(1.0, prob))
    except ValueError:
        return None


def parse_dive_mode(text: str):
    """解析潜水/记录开关指令，返回 True 表示开启，False 表示关闭，None 表示未匹配"""
    if re.search(r'开启(潜水|记录)', text):
        return True
    elif re.search(r'关闭(潜水|记录)', text):
        return False
    return None


def extract_at_ids(event: GroupMessageEvent):
    """从事件消息中提取所有 at 的 QQ 号（int）"""
    at_ids = []
    for seg in event.message:
        if seg.type == "at":
            qq = seg.data.get("qq")
            if qq and qq != "all":
                try:
                    at_ids.append(int(qq))
                except (ValueError, TypeError):
                    pass
    return at_ids


@admin_hard.handle()
async def handle_hard_admin(bot: Bot, event: GroupMessageEvent, text: str = EventPlainText()):
    text = text.strip()
    user_id = str(event.user_id)
    group_id = event.group_id

    # 基础权限检查
    try:
        member_info = await bot.get_group_member_info(group_id=group_id, user_id=event.user_id)
        role = member_info.get("role", "member")
    except:
        role = "member"

    if not perm_srv.has_command_privilege(user_id, role):
        return

    # ---------- 原有指令：退群 ----------
    if text in ["退群", "leave"]:
        await admin_hard.send("收到指令，正在退出群聊...")
        await bot.set_group_leave(group_id=group_id)
        await admin_hard.finish()

    # ---------- 新增指令 A：活跃度调整 ----------
    prob = parse_prob(text)
    if prob is not None:
        gid_str = str(group_id)
        gcfg = plugin_config.group_configs.setdefault(gid_str, GroupSettings())
        gcfg.random_reply_prob = prob
        try:
            plugin_config.save_config()
            await admin_hard.send(f"✅ 已将本群随机插嘴概率调整为 {prob*100:.1f}%")
        except Exception as e:
            await admin_hard.send(f"❌ 保存配置失败: {e}")
        await admin_hard.finish()

    # ---------- 新增指令 B：潜水模式切换 ----------
    dive_flag = parse_dive_mode(text)
    if dive_flag is not None:
        gid_str = str(group_id)
        gcfg = plugin_config.group_configs.setdefault(gid_str, GroupSettings())
        gcfg.record_all_messages = dive_flag
        try:
            plugin_config.save_config()
            mode_text = "已开启" if dive_flag else "已关闭"
            await admin_hard.send(f"✅ 潜水记录模式 {mode_text}")
        except Exception as e:
            await admin_hard.send(f"❌ 保存配置失败: {e}")
        await admin_hard.finish()

    # ---------- 新增指令 C：白名单授权（画图） ----------
    if re.search(r'(开启画图白名单|授权画图)', text):
        at_ids = extract_at_ids(event)
        if not at_ids:
            await admin_hard.send("❌ 请 @ 需要授权的用户")
            await admin_hard.finish()
        new_users = set()
        for qq in at_ids:
            plugin_config.drawing_whitelist.add(str(qq))
            new_users.add(str(qq))
        try:
            plugin_config.save_config()
            names = ", ".join([f"QQ({q})" for q in new_users])
            await admin_hard.send(f"✅ 已为 {names} 解锁画图模块")
        except Exception as e:
            await admin_hard.send(f"❌ 保存配置失败: {e}")
        await admin_hard.finish()
