import asyncio
import time

from nonebot import get_bot
from nonebot.log import logger

from ..config import plugin_config

# 全局冷却状态
_lock = asyncio.Lock()
_last_alert_time: float = 0.0
COOLDOWN_SECONDS: float = 3600.0  # 1 小时


def reset_cooldown() -> None:
    """重置告警冷却时间。系统恢复健康后调用，使下次故障可立刻告警。"""
    global _last_alert_time
    _last_alert_time = 0.0


async def send_emergency_alert(message: str) -> None:
    """
    向所有超级管理员发送紧急私聊告警。
    冷却时间 1 小时，防止刷屏。发送失败仅记录日志，不抛异常。
    """
    global _last_alert_time

    async with _lock:
        now = time.monotonic()
        if now - _last_alert_time < COOLDOWN_SECONDS:
            logger.info("[AlertManager] 告警处于冷却，跳过发送")
            return
        _last_alert_time = now

    try:
        bot = get_bot()
    except Exception:
        logger.error("[AlertManager] Bot 未连接，无法发送告警")
        return

    superusers = plugin_config.superusers
    if not superusers:
        logger.warning("[AlertManager] 未配置 superusers，无法发送告警")
        return

    for uid in superusers:
        try:
            await bot.send_private_msg(user_id=int(uid), message=message)
        except Exception as e:
            logger.error(f"[AlertManager] 向 {uid} 发送告警失败: {e}")
