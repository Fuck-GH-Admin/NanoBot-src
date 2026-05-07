# src/plugins/chatbot/tools/system_tool.py

from typing import Any, Dict, List, Tuple
from .base_tool import BaseTool


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
