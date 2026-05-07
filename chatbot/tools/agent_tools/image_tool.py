# src/plugins/chatbot/tools/agent_tools/image_tool.py

from typing import Any, Dict, List, Tuple
from ..base_tool import BaseTool


class GenerateImageTool(BaseTool):
    name = "generate_image"
    is_write_operation = True
    description = '【条件触发】：当用户明确要求"画一张图"、"生成一张XX"时，**绝对禁止用文字假装画图**，必须输出 tool_call 调用此工具以生成真实图片！'
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "图片描述，请用英文或中文详细描述画面内容"
            }
        },
        "required": ["prompt"]
    }
    require_permission = "drawing_whitelist"
    risk_level = "low"
    allow_forced_exec = False

    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[str]]:
        prompt = arguments.get("prompt", "").strip()
        if not prompt:
            return "错误：缺少图片描述", []

        # 从上下文获取绘图服务
        drawing_srv = context.get("drawing_service")
        if not drawing_srv:
            from ...services.drawing_service import DrawingService
            drawing_srv = DrawingService()

        path, msg = await drawing_srv.generate_image(prompt, context["user_id"])
        images = [path] if path else []
        return msg, images


class SearchAcgImageTool(BaseTool):
    name = "search_acg_image"
    description = '【条件触发】：当用户要求"发张图"、"来点色图/涩图"、"看图"时，**绝对禁止用文字敷衍**，必须输出 tool_call 调用此工具去图库搜索真实图片！'
    parameters = {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "string",
                "description": "搜索关键词，多个词用空格分隔。留空则随机推荐"
            },
            "allow_r18": {
                "type": "boolean",
                "description": "是否允许返回 R18 内容，默认为 false"
            }
        },
        "required": []
    }
    require_permission = "user"
    risk_level = "low"
    allow_forced_exec = True

    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[str]]:
        keywords = arguments.get("keywords", "").strip()
        allow_r18 = arguments.get("allow_r18", False)
        # 最终的 R18 权限需结合群聊设置
        actual_r18 = context.get("allow_r18", False) and allow_r18

        image_srv = context.get("image_service")
        if not image_srv:
            from ...services.image_service import ImageService
            image_srv = ImageService()

        path, info = await image_srv.get_image(keywords, actual_r18)
        images = [path] if path else []
        return info, images
