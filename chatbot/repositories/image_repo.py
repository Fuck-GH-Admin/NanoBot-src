import json
import os
import re
import aiosqlite
from typing import List, Dict, Optional
from pathlib import Path
from nonebot.log import logger

from ..config import plugin_config

# PixivUtil2 标准图像后缀
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# 从 save_name 路径中提取画师名和画师 ID 的正则
# 匹配模式: .../{Artist Name} ({Artist ID})/...
_ARTIST_DIR_RE = re.compile(r'[/\\]([^/\\]+)\s+\((\d+)\)[/\\]')


def _extract_artist_from_path(save_name: str) -> tuple[Optional[int], Optional[str]]:
    """从 PixivUtil2 的 save_name 路径中提取 (artist_id, artist_name)。"""
    if not save_name:
        return None, None
    m = _ARTIST_DIR_RE.search(save_name)
    if m:
        return int(m.group(2)), m.group(1)
    return None, None


def resolve_pixiv_image_path(
    image_id: int,
    save_name: Optional[str] = None,
    artist_id: Optional[int] = None,
    artist_name: Optional[str] = None,
) -> Optional[Path]:
    """
    高鲁棒性 Pixiv 图片物理路径解析器。

    寻址策略（混合模式）：
    1. 直接路径试探：若 save_name 为绝对路径且文件存在，直接返回。
    2. 最优路径拼接：若已知 artist_id + artist_name，拼接标准目录结构。
    3. 回退全局搜索：rglob 匹配 {image_id}_p*.*，取第一个有效图片。

    :return: 解析成功的绝对 Path，或 None（文件不存在 / 未命中）。
    """
    image_folder = Path(plugin_config.image_folder)

    # --- 策略 1：直接路径试探 ---
    if save_name:
        direct = Path(save_name)
        if direct.is_absolute() and direct.exists():
            return direct.resolve()
        # 也尝试相对于 image_folder 的路径
        relative = image_folder / save_name
        if relative.exists():
            return relative.resolve()

    # --- 策略 2：最优路径拼接（O(1) 试探） ---
    if artist_id and artist_name:
        artist_dir = image_folder / f"{artist_name} ({artist_id})"
        if artist_dir.is_dir():
            # 匹配 {image_id}_p*.{ext}
            for suffix in _IMAGE_SUFFIXES:
                # 尝试 _p0, _p1, ... 直接命中
                candidate = artist_dir / f"{image_id}_p0{suffix}"
                if candidate.exists():
                    return candidate.resolve()
            # 通配：任意页码
            matches = list(artist_dir.glob(f"{image_id}_p*.*"))
            img_matches = [m for m in matches if m.suffix.lower() in _IMAGE_SUFFIXES]
            if img_matches:
                return img_matches[0].resolve()

    # --- 策略 3：回退全局搜索 ---
    try:
        pattern = f"*{image_id}_p*.*"
        fallback_root = image_folder
        if not fallback_root.is_dir():
            logger.warning(f"[ImageRepo] image_folder 不存在: {fallback_root}")
            return None
        matches = list(fallback_root.rglob(pattern))
        img_matches = [m for m in matches if m.suffix.lower() in _IMAGE_SUFFIXES]
        if img_matches:
            return img_matches[0].resolve()
    except Exception as e:
        logger.warning(f"[ImageRepo] rglob 回退搜索失败 (id={image_id}): {e}")

    logger.warning(f"[ImageRepo] 未找到 image_id={image_id} 的物理文件 (save_name={save_name})")
    return None


class ImageRepository:
    """
    图片元数据仓库
    基于 SQLite (aiosqlite) 查询 pixiv_master_image + pixiv_ai_info，
    结合本地 JSON sidecar 文件解析标签与画师信息。
    """

    def __init__(self):
        self.db_path: str = plugin_config.db_path
        self.image_folder: str = plugin_config.image_folder

    def _get_json_path(self, image_path: Path) -> Optional[Path]:
        """从图片路径推导 JSON sidecar 路径 (xxx_p.json)"""
        # 11403_p0.jpg -> 11403_p.json
        stem = image_path.stem  # 11403_p0
        if '_p' in stem:
            base = stem.rsplit('_p', 1)[0]
            json_name = f"{base}_p.json"
            return image_path.parent / json_name
        return None

    def _read_sidecar(self, image_path: Path) -> Dict:
        """读取 JSON sidecar 文件，返回 {"tags": [...], "artist": str}"""
        json_path = self._get_json_path(image_path)
        if json_path and json_path.exists():
            try:
                with open(json_path, 'rb') as f:
                    data = json.loads(f.read().decode('utf-8'))
                return {
                    "tags": (data.get("Tags") or []),
                    "artist": data.get("Artist Name") or "",
                }
            except Exception:
                pass
        return {"tags": [], "artist": ""}

    def _build_info_text(self, title: str, sidecar: Dict, is_ai: bool, image_id: int) -> str:
        """构建图片描述信息"""
        ai_str = "是" if is_ai else "否"
        artist = sidecar.get("artist", "") or "N/A"
        tag_str = ", ".join(str(t) for t in (sidecar.get("tags") or []))
        return (
            f"画师: {artist}\n"
            f"图片ID: {image_id}\n"
            f"标题: {title or 'N/A'}\n"
            f"标签: {tag_str or 'N/A'}\n"
            f"AI: {ai_str}"
        )

    async def query_images(
        self,
        keywords: List[str],
        classification: Optional[str] = None,
        no_ai: bool = False,
        limit: int = 50,
    ) -> List[Dict[str, str]]:
        """
        查询图片
        :param keywords: 关键词列表 (OR 逻辑，匹配标题或标签)
        :param classification: 分类筛选 (R18, R18G)
        :param no_ai: 是否过滤 AI 作品
        :param limit: 最大返回数量
        :return: 图片信息列表 [{"path": str, "info": str, "uid": str, "tags": str}]
        """
        if not os.path.exists(self.db_path):
            logger.warning(f"[ImageRepo] DB not found: {self.db_path}")
            return []

        try:
            async with aiosqlite.connect(self.db_path) as db:
                # 构建 SQL 查询
                # LEFT JOIN pixiv_ai_info 以支持 AI 过滤
                # 注意：pixiv_master_member 可能为空，画师信息从 save_name 路径中提取
                sql = """
                    SELECT i.image_id, i.title, i.save_name, a.ai_type,
                           i.member_id
                    FROM pixiv_master_image i
                    LEFT JOIN pixiv_ai_info a ON i.image_id = a.image_id
                """
                conditions = []
                params = []

                # AI 过滤（数据库级别）
                if no_ai:
                    conditions.append("(a.ai_type IS NULL OR a.ai_type = 0)")

                # 关键词过滤（匹配标题）
                if keywords:
                    kw_conds = []
                    for kw in keywords:
                        kw_conds.append("i.title LIKE ?")
                        params.append(f"%{kw}%")
                    conditions.append(f"({' OR '.join(kw_conds)})")

                if conditions:
                    sql += " WHERE " + " AND ".join(conditions)

                # 无关键词时随机排序，有关键词时按 rowid（近似插入序）
                if keywords:
                    sql += " ORDER BY i.image_id DESC"
                else:
                    sql += " ORDER BY RANDOM()"

                # 多取一些，因为后续还要过滤不存在的文件和 R18
                sql += " LIMIT ?"
                params.append(limit * 5)

                cursor = await db.execute(sql, params)
                rows = await cursor.fetchall()

                results = []
                for row in rows:
                    if len(results) >= limit:
                        break

                    image_id, title, save_name, ai_type, member_id = row

                    # 从 save_name 路径中提取画师信息（pixiv_master_member 可能为空）
                    path_artist_id, path_artist_name = _extract_artist_from_path(save_name or "")
                    artist_id = path_artist_id or member_id
                    artist_name = path_artist_name

                    # 物理路径解析
                    resolved = resolve_pixiv_image_path(
                        image_id, save_name,
                        artist_id=artist_id, artist_name=artist_name,
                    )
                    if resolved is None:
                        continue

                    resolved_str = str(resolved)
                    is_ai = ai_type is not None and ai_type != 0

                    # 读取 JSON sidecar（获取标签和画师信息）
                    sidecar = self._read_sidecar(resolved)

                    # 分类过滤（R18/R18G 基于标签）
                    if classification:
                        tags_lower = [str(t).lower() for t in (sidecar.get("tags") or [])]
                        if classification.upper() == "R18":
                            if not any("r-18" in t and "r-18g" not in t for t in tags_lower):
                                continue
                        elif classification.upper() == "R18G":
                            if not any("r-18g" in t for t in tags_lower):
                                continue

                    # 关键词二次匹配（标题 + 标签，SQL 只匹配了标题）
                    if keywords:
                        kw_lower = [k.lower() for k in keywords]
                        title_lower = (title or "").lower()
                        tags_joined = " ".join(str(t) for t in (sidecar.get("tags") or [])).lower()
                        artist_name = sidecar.get("artist", "").lower()
                        searchable = f"{title_lower} {tags_joined} {artist_name}"
                        if not any(kw in searchable for kw in kw_lower):
                            continue

                    info_text = self._build_info_text(title, sidecar, is_ai, image_id)
                    tag_str = ", ".join(str(t) for t in (sidecar.get("tags") or []))

                    results.append({
                        "path": resolved_str,
                        "info": info_text,
                        "uid": str(image_id),
                        "tags": tag_str,
                    })

                return results

        except Exception as e:
            logger.error(f"[ImageRepo] Query failed: {e}")
            return []

    async def get_image_by_id(self, uid: str) -> Optional[Dict]:
        """根据图片 ID 精确获取图片"""
        if not os.path.exists(self.db_path):
            return None

        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    """
                    SELECT i.image_id, i.title, i.save_name, a.ai_type,
                           i.member_id
                    FROM pixiv_master_image i
                    LEFT JOIN pixiv_ai_info a ON i.image_id = a.image_id
                    WHERE i.image_id = ?
                    """,
                    (int(uid),),
                )
                row = await cursor.fetchone()
                if not row:
                    return None

                image_id, title, save_name, ai_type, member_id = row

                # 从 save_name 路径中提取画师信息
                path_artist_id, path_artist_name = _extract_artist_from_path(save_name or "")
                artist_id = path_artist_id or member_id
                artist_name = path_artist_name

                # 物理路径解析
                resolved = resolve_pixiv_image_path(
                    image_id, save_name,
                    artist_id=artist_id, artist_name=artist_name,
                )
                if resolved is None:
                    return None

                is_ai = ai_type is not None and ai_type != 0
                sidecar = self._read_sidecar(resolved)
                info_text = self._build_info_text(title, sidecar, is_ai, image_id)

                return {
                    "path": str(resolved),
                    "info": info_text,
                    "uid": str(image_id),
                }

        except Exception as e:
            logger.error(f"[ImageRepo] get_image_by_id failed: {e}")
            return None
