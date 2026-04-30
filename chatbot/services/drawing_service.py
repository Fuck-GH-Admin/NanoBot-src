# src/plugins/chatbot/services/drawing_service.py

import httpx
import asyncio
from typing import Tuple
from pathlib import Path
from datetime import datetime
from nonebot.log import logger

from ..config import plugin_config


class DrawingService:
    """
    AI 绘图服务
    整合 Prompt 优化与 SiliconFlow API 调用（优化环节已改用 Node.js 大脑）
    """

    def __init__(self):
        self.image_dir = Path("data/generated_images")
        self.image_dir.mkdir(parents=True, exist_ok=True)

        self.api_key = plugin_config.siliconflow_api_key
        self.api_url = plugin_config.siliconflow_api_url
        # Node.js 对话接口地址（用于 prompt 优化）
        self.node_chat_url = plugin_config.node_chat_url

    async def generate_image(self, simple_prompt: str, user_id: str) -> Tuple[str, str]:
        """
        生成图片的主入口
        :return: (本地文件路径, 提示信息)
        """
        # 1. 权限检查 (白名单)
        if user_id not in plugin_config.drawing_whitelist:
            return "", "❌ 你没有绘图权限，请联系管理员~"

        # 2. 提示词优化 (通过 Node.js 大脑)
        logger.info(f"Optimizing prompt: {simple_prompt}")
        try:
            enhanced_prompt = await asyncio.wait_for(
                self._enhance_prompt(simple_prompt),
                timeout=plugin_config.drawing_enhance_timeout
            )
        except asyncio.TimeoutError:
            enhanced_prompt = simple_prompt  # 降级策略
        except Exception as e:
            logger.error(f"Prompt enhancement failed: {e}")
            enhanced_prompt = simple_prompt

        logger.info(f"Enhanced: {enhanced_prompt}")

        # 3. 调用绘图 API
        try:
            path = await self._call_siliconflow(enhanced_prompt, user_id)
            if path:
                return path, f"✅ 绘图完成！\nPrompt: {enhanced_prompt[:30]}..."
            else:
                return "", "❌ 生成失败，API 返回了错误。"
        except Exception as e:
            logger.error(f"API Error: {e}")
            return "", f"❌ 发生错误: {e}"

    async def _enhance_prompt(self, simple_prompt: str) -> str:
        """
        调用 Node.js 大脑优化绘图提示词
        """
        messages = [
            {
                "role": "user",
                "content": (
                    "你是一个专业的AI绘画提示词工程师。"
                    "请将以下简短描述扩展为一段详细、高质量的英文绘图提示词，加入对画风、光线、构图、细节的描述。"
                    f"只输出最终的英文提示词，不要额外解释。\n简短描述：{simple_prompt}"
                )
            }
        ]
        payload = {
            "chatHistory": messages,
            "tools": [],          # 不需要工具调用
            "user_id": "drawing_optimizer"
        }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(self.node_chat_url, json=payload)
            if resp.status_code != 200:
                logger.error(f"Enhance prompt API error {resp.status_code}: {resp.text}")
                return simple_prompt

            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", simple_prompt)
        return simple_prompt

    async def _call_siliconflow(self, prompt: str, user_id: str) -> str:
        """
        异步调用 SiliconFlow API
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": plugin_config.siliconflow_model_name,
            "prompt": prompt,
            "image_size": "1024x1024",
            "num_inference_steps": 20
        }

        async with httpx.AsyncClient() as session:
            # 1. 请求生成
            async with session.post(
                f"{self.api_url}/images/generations",
                json=payload,
                headers=headers
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error(f"API Error {resp.status}: {err}")
                    return ""

                data = await resp.json()

                data_list = data.get("data", [])
                if data_list and isinstance(data_list, list):
                    img_url = data_list[0].get("url")
                else:
                    img_url = None

                if not img_url:
                    logger.error(f"API returned no URL. Raw data: {data}")
                    return ""

            # 2. 下载图片
            async with session.get(img_url) as img_resp:
                if img_resp.status == 200:
                    content = await img_resp.read()

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"{user_id}_{timestamp}.png"
                    file_path = self.image_dir / filename

                    with open(file_path, "wb") as f:
                        f.write(content)

                    return str(file_path.resolve())
        return ""