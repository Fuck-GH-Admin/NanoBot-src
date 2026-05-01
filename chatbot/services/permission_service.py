# src/plugins/chatbot/services/permission_service.py

import re
import json
import httpx
from typing import Set, List, Literal
from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger

from ..config import plugin_config


class PermissionService:
    """
    权限与群管理核心服务

    职责：
    1. 统一鉴权：Superuser / AI Admin / Group Admin 判定。
    2. 执行管理操作：封装 Ban/Kick 等 API 调用，包含前置检查。
    3. 智能审计：基于 Node.js 大脑的违规内容判定。
    """

    def __init__(self):
        # 加载配置中的静态名单
        self.superusers: Set[str] = plugin_config.superusers
        self.ai_admin_qq: Set[str] = plugin_config.ai_admin_qq
        self.private_whitelist: Set[str] = plugin_config.private_whitelist
        self.drawing_whitelist: Set[str] = plugin_config.drawing_whitelist
        # Node.js 对话接口地址（用于 AI 审计）
        self.node_chat_url = plugin_config.node_chat_url

    # ================= 基础鉴权逻辑 =================

    def is_superuser(self, user_id: str) -> bool:
        return user_id in self.superusers

    def is_ai_admin(self, user_id: str) -> bool:
        return user_id in self.ai_admin_qq

    def is_group_admin(self, sender_role: str) -> bool:
        return sender_role in ["owner", "admin"]

    def has_command_privilege(self, user_id: str, sender_role: str) -> bool:
        return (
            self.is_superuser(user_id)
            or self.is_ai_admin(user_id)
            or self.is_group_admin(sender_role)
        )

    def is_user_whitelisted(self, user_id: str, scope: Literal["private", "drawing"]) -> bool:
        if scope == "private":
            return user_id in self.private_whitelist
        elif scope == "drawing":
            return user_id in self.drawing_whitelist
        return False

    def is_private_whitelisted(self, user_id: str) -> bool:
        return self.is_user_whitelisted(user_id, "private")

    # ================= 业务执行逻辑 =================

    async def check_bot_admin_status(self, bot: Bot, group_id: int) -> bool:
        """检查 Bot 自身在群内是否具有管理员权限"""
        try:
            # 尝试使用 no_cache（go-cqhttp 扩展），失败则降级为标准调用
            bot_member = await bot.get_group_member_info(
                group_id=group_id,
                user_id=int(bot.self_id),
                no_cache=True
            )
        except Exception:
            try:
                bot_member = await bot.get_group_member_info(
                    group_id=group_id,
                    user_id=int(bot.self_id)
                )
            except Exception as e:
                logger.error(f"[Permission] Failed to check bot status: {e}")
                return False
        return bot_member.get("role") in ["owner", "admin"]

    async def ban_user(
        self,
        bot: Bot,
        group_id: int,
        target_id: int,
        duration: int,
        operator_id: str,
        reason: str = "Admin Command"
    ) -> str:
        """执行禁言操作（含层级检查）"""
        if not await self.check_bot_admin_status(bot, group_id):
            return "❌ 我没有管理员权限，无法执行禁言操作。"

        try:
            target_info = await bot.get_group_member_info(group_id=group_id, user_id=target_id)
            if target_info.get("role") in ["owner", "admin"]:
                return f"❌ 无法禁言管理员 <{target_id}>，对方权限等级过高。"
        except Exception:
            pass

        try:
            await bot.set_group_ban(
                group_id=group_id,
                user_id=target_id,
                duration=duration
            )
            logger.info(f"Op:{operator_id} -> Target:{target_id} | Dur:{duration}s | Reason:{reason}")

            if duration == 0:
                return f"✅ 已解除 <{target_id}> 的禁言。"
            else:
                return f"🚫 用户 <{target_id}> 已被禁言 {duration}秒。"
        except Exception as e:
            logger.error(f"Execution failed: {e}")
            return f"❌ 操作失败: {str(e)}"

    async def kick_user(self, bot: Bot, group_id: int, target_id: int, reject_add_request: bool = False) -> str:
        """执行踢人操作"""
        if not await self.check_bot_admin_status(bot, group_id):
            return "❌ 我没有管理员权限，无法踢人。"

        try:
            target_info = await bot.get_group_member_info(group_id=group_id, user_id=target_id)
            if target_info.get("role") in ["owner", "admin"]:
                return "❌ 无法踢出群管理员。"

            await bot.set_group_kick(
                group_id=group_id,
                user_id=target_id,
                reject_add_request=reject_add_request
            )
            return f"👋 已将 <{target_id}> 移出群聊。"
        except Exception as e:
            logger.error(f"[Kick] Execution failed: {e}")
            return f"❌ 踢人失败: {e}"

    async def _fetch_group_history(self, bot: Bot, group_id: int) -> list:
        """拉取群历史消息。当前依赖 go-cqhttp 扩展 API 'get_group_msg_history'。"""
        try:
            history = await bot.call_api("get_group_msg_history", group_id=group_id)
            return history.get("messages", [])
        except Exception as e:
            logger.warning(f"[Audit] Fetch history failed: {e}")
            raise

    async def ai_audit_and_punish(self, bot: Bot, group_id: int, target_id: int) -> str:
        """AI 智能审计：拉取用户最近发言，判断是否违规，若违规自动禁言。"""
        try:
            messages = await self._fetch_group_history(bot, group_id)
        except Exception:
            return "❌ 获取聊天记录失败，无法进行 AI 审计。"

        user_msgs = []
        for msg in messages:
            if str(msg.get("user_id")) == str(target_id):
                text_content = ""
                if isinstance(msg.get("message"), list):
                    for seg in msg["message"]:
                        if seg["type"] == "text":
                            text_content += seg["data"].get("text", "")
                elif isinstance(msg.get("message"), str):
                    text_content = msg["message"]

                if text_content:
                    user_msgs.append(text_content)

        if not user_msgs:
            return "❓ 该用户近期没有发言记录，AI 无法判断。"

        evidence = "\n".join(user_msgs[:15])

        prompt = (
            "请审核以下用户的群聊发言，判断是否存在严重违规（如：色情、暴力、诈骗、恶意刷屏、人身攻击）。"
            "忽略轻微的玩笑。直接返回JSON格式：{\"violation\": true/false, \"reason\": \"原因\", \"suggested_duration\": 秒数}\n"
            f"用户发言：\n{evidence}"
        )

        try:
            response_text = await self._call_node_chat(prompt)

            match = re.search(r"\{.*\}", response_text, re.DOTALL)
            if match:
                result = json.loads(match.group(0))
            else:
                return "❌ AI 返回格式异常，审计中止。"

            if result.get("violation"):
                duration = result.get("suggested_duration", 600)
                reason = result.get("reason", "AI判定违规")
                ban_msg = await self.ban_user(bot, group_id, target_id, duration, "AI_AUDIT", reason)
                return f"🤖 AI 审计判定违规。\n理由：{reason}\n执行结果：{ban_msg}"
            else:
                return f"🤖 AI 审计判定未违规。\n理由：{result.get('reason')}"

        except Exception as e:
            logger.error(f"[Audit] AI processing failed: {e}")
            return f"❌ AI 审计过程出错: {e}"

    async def _call_node_chat(self, user_prompt: str) -> str:
        """
        调用 Node.js 大脑进行简单对话（不涉及工具）
        """
        messages = [{"role": "user", "content": user_prompt}]
        payload = {
            "chatHistory": messages,
            "tools": [],
            "user_id": "audit_system"
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(self.node_chat_url, json=payload)
                if resp.status_code != 200:
                    logger.error(f"Audit API error {resp.status_code}: {resp.text}")
                    return ""
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.error(f"Audit node call failed: {e}")
        return ""

    def parse_duration(self, text: str) -> int:
        """解析自然语言时间描述。"""
        if not text:
            return 1800

        text = text.lower().strip()
        multipliers = {
            "秒": 1, "s": 1, "sec": 1,
            "分": 60, "min": 60, "m": 60,
            "小时": 3600, "hour": 3600, "h": 3600, "钟": 3600,
            "天": 86400, "day": 86400, "d": 86400
        }

        match = re.search(r'(\d+)\s*([一-龥a-z]+)?', text)
        if match:
            value = int(match.group(1))
            unit = match.group(2)

            if not unit:
                return value * 60

            for key, mult in multipliers.items():
                if key in unit:
                    return min(value * mult, 2592000)
        return 1800
