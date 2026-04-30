import os
import random
import time
import re
import hashlib
from typing import Tuple, Optional, List
from datetime import datetime
from PIL import Image, PngImagePlugin
from io import BytesIO
from pathlib import Path
from nonebot.log import logger

from ..repositories.image_repo import ImageRepository
from ..config import plugin_config

class ImageService:
    """
    图片服务
    负责图片检索、分类过滤以及抗风控处理（Stealth Processing）
    """
    def __init__(self):
        self.repo = ImageRepository()
        self.stealth_dir = Path("data/stealth_images")
        self.stealth_dir.mkdir(parents=True, exist_ok=True)

    async def get_image(self, text: str, allow_r18: bool = False) -> Tuple[Optional[str], str]:
        """
        获取单张图片（包含搜索逻辑）
        返回: (文件路径, 信息文本)
        """
        # 1. 解析参数
        keywords, classification, no_ai = self._parse_intent(text, allow_r18)
        
        # 2. 查询仓库
        results = self.repo.query_images(keywords, classification, no_ai)
        
        if not results:
            mode_str = classification if classification else "全年龄"
            return None, f"没有找到相关图片哦~ (模式: {mode_str})"
        
        # 3. 随机选择一张
        # 如果有关键词，results已经是按匹配度排序的，可以取前几个随机
        target = random.choice(results[:5]) if keywords else random.choice(results)
        
        return target["path"], target["info"]

    async def get_multi_images(self, text: str, allow_r18: bool) -> Tuple[List[str], str]:
        """
        解析 '图1-3' 或 '图1 图2' 等多图指令
        """
        has_search, keywords, indices = self._parse_search_keywords(text)
        if not has_search or not indices:
            # 回退到单图逻辑
            path, info = await self.get_image(text, allow_r18)
            return ([path] if path else []), info

        # 执行精确搜索
        classification = "Artist" # 默认
        if allow_r18:
            if "r18g" in text.lower(): classification = "R18G"
            elif "r18" in text.lower() or "涩图" in text: classification = "R18"

        results = self.repo.query_images(keywords, classification, "不要ai" in text, limit=100)
        
        if not results:
            return [], "未找到符合关键词的图片"

        # 提取指定索引的图片
        selected_paths = []
        info_lines = [f"找到 {len(results)} 张相关图片，已选："]
        
        valid_indices = [i for i in indices if 1 <= i <= len(results)]
        for idx in valid_indices:
            item = results[idx - 1]
            selected_paths.append(item["path"])
            info_lines.append(f"{idx}. ID: {item['uid']}")

        return selected_paths, "\n".join(info_lines)

    async def generate_stealth(self, original_path: str, strategy: int = 0) -> str:
        """
        生成抗风控副本
        移植自原 image_handler.py，包含多种噪声与元数据注入策略
        """
        try:
            # 简单缓存检查
            hash_name = hashlib.md5(f"{original_path}_{strategy}".encode()).hexdigest()
            out_path = self.stealth_dir / f"stealth_{hash_name}.png"
            if out_path.exists():
                return str(out_path)

            img = Image.open(original_path).convert("RGBA")
            w, h = img.size

            # === 策略实现 ===
            save_kwargs = {}
            
            if strategy == 0: # 微调像素 + 元数据
                pixels = img.load()
                if w > 10 and h > 10:
                    r, g, b, a = pixels[w-5, h-5]
                    pixels[w-5, h-5] = ((r + 1) % 256, g, b, a)
                
                info = PngImagePlugin.PngInfo()
                info.add_text("GenTime", str(time.time()))
                info.add_text("Nonce", str(random.randint(1000,9999)))
                save_kwargs["pnginfo"] = info

            elif strategy == 1: # 稀疏噪点
                pixels = img.load()
                for x in range(0, w, 30):
                    for y in range(0, h, 30):
                        r, g, b, a = pixels[x, y]
                        pixels[x, y] = (r, g, b, max(0, a - 1)) # 修改透明度微小值

            elif strategy == 2: # 微旋转
                img = img.rotate(0.1, resample=Image.BICUBIC, expand=False)
            
            elif strategy == 3: # 格式转换重编码 (JPEG -> PNG)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=95)
                img = Image.open(buf).convert("RGBA")

            # 保存
            img.save(out_path, "PNG", **save_kwargs)
            logger.info(f"[ImageService] Generated stealth image: {out_path.name}")
            return str(out_path)

        except Exception as e:
            logger.error(f"[ImageService] Stealth generation failed: {e}")
            return original_path

    def _parse_intent(self, text: str, allow_r18: bool) -> Tuple[List[str], Optional[str], bool]:
        """解析文本中的：关键词、分类要求、AI过滤要求"""
        text_lower = text.lower()
        
        # 1. 判定分类
        classification = "Artist"
        if allow_r18:
            if any(k in text_lower for k in ["r18g", "极端", "重口"]):
                classification = "R18G"
            elif any(k in text_lower for k in ["r18", "涩图", "色图"]):
                classification = "R18"
        
        # 2. 判定AI
        no_ai = "不要ai" in text_lower
        
        # 3. 提取关键词 (移除触发词)
        triggers = ["发图", "来张图", "涩图", "色图", "r18", "r18g", "图", "不要ai"]
        clean_text = text
        for t in triggers:
            clean_text = re.sub(t, "", clean_text, flags=re.IGNORECASE)
        
        keywords = [k.strip() for k in clean_text.split() if k.strip()]
        
        return keywords, classification, no_ai

    def _parse_search_keywords(self, text: str) -> Tuple[bool, List[str], Optional[List[int]]]:
        """解析 '图1-3' 这种高级搜索语法 (完全保留原逻辑)"""
        text_lower = text.lower()
        image_indices = []
        original_text = text

        # 模式1: 连续范围 "图3-图5"
        range_pattern = r'(图|pic)\s*(\d+)\s*(?:-|到)\s*(?:(图|pic)\s*)?(\d+)'
        for match in re.finditer(range_pattern, text_lower):
            start, end = int(match.group(2)), int(match.group(4))
            if start > end: start, end = end, start
            image_indices.extend(range(start, end + 1))
            original_text = original_text.replace(match.group(0), '', 1)

        # 模式2: 单个 "图3"
        single_pattern = r'(图|pic)\s*(\d+)'
        for match in re.finditer(single_pattern, original_text.lower()):
            image_indices.append(int(match.group(2)))
            original_text = original_text.replace(match.group(0), '', 1)

        image_indices = sorted(set(image_indices))
        
        # 提取剩余关键词
        triggers = ["发图", "来张图", "涩图", "r18"]
        for t in triggers:
            original_text = re.sub(t, "", original_text, flags=re.IGNORECASE)
        
        keywords = [k.strip() for k in original_text.split() if k.strip()]
        
        return bool(image_indices), keywords, (image_indices if image_indices else None)