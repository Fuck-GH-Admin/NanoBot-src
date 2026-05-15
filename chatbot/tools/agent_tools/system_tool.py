# src/plugins/chatbot/tools/agent_tools/system_tool.py

from typing import Any, Dict, List, Tuple
from nonebot.log import logger
from ..base_tool import BaseTool


class MarkTaskCompleteTool(BaseTool):
    name = "mark_task_complete"
    description = "当你认为当前用户请求已经完成时，调用此工具以结束任务循环。"
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "对完成情况的简要总结"
            }
        },
        "required": ["summary"]
    }
    require_permission = "system"
    risk_level = "low"
    allow_forced_exec = False

    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[str]]:
        return f"任务完成: {arguments.get('summary', '')}", []


class NoOpTool(BaseTool):
    name = "no_op"
    description = (
        "当你判断当前用户的输入只是日常闲聊、情感交流、或者不需要任何后台操作（如搜索、画图、下载）时，"
        "必须调用此工具以结束当前调度回合。这代表将响应权限交由『人格脑』处理。"
    )
    parameters = {
        "type": "object",
        "properties": {}
    }
    require_permission = "system"
    risk_level = "low"
    allow_forced_exec = False

    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[str]]:
        return "确认无后台操作，状态已流转至人格层。", []


class ExitSessionTool(BaseTool):
    """
    沉浸会话退出工具。

    由逻辑脑在语义判断用户意图后显式调用，用于销毁当前用户的沉浸会话状态。
    执行后，人格脑将被短路（不渲染），实现静默退出。
    """
    name = "exit_session"
    description = (
        "结束当前与用户的沉浸式对话会话。"
        "当用户的话题与你无关、用户正在与群内其他人互动、"
        "或用户表达了明确的结束/拒绝意图时，调用此工具。"
        "调用后你将不再主动响应该用户的后续消息，除非用户再次 @ 你或使用唤醒词。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "非必填。如需填写，用最简短的词（如'闲聊'、'指令结束'），禁止超过10个字符。"
            }
        },
        "required": []
    }
    require_permission = "system"
    risk_level = "low"
    allow_forced_exec = False
    is_write_operation = False

    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[str]]:
        reason = arguments.get("reason", "未指定")
        group_id = context.get("group_id", 0)
        user_id = context.get("user_id", "")

        is_tome = context.get("is_tome", False)
        is_reply_bot = context.get("is_reply_bot", False)
        has_wake_word = context.get("has_wake_word", False)

        # 通过 context 中注入的会话管理器销毁沉浸会话（topic 粒度）
        active_sessions = context.get("_active_sessions")
        if active_sessions and group_id:
            group_topics = active_sessions.get(group_id, {})
            found = False
            for tid, users in group_topics.items():
                if user_id in users:
                    del users[user_id]
                    found = True
                    logger.info(
                        f"[ExitSession] 已销毁群 {group_id} 话题 {tid} 用户 {user_id} 的沉浸会话。"
                        f"原因: {reason}, is_tome={is_tome}, is_reply_bot={is_reply_bot}, has_wake_word={has_wake_word}"
                    )
                    break
            if found:
                return "会话已退出。", []
            else:
                logger.debug(f"[ExitSession] 用户 {user_id} 无活跃沉浸会话，跳过")
                return "当前无活跃会话。", []
        else:
            logger.warning("[ExitSession] 上下文缺少 _active_sessions 引用")
            return "会话退出失败：缺少会话管理器。", []
