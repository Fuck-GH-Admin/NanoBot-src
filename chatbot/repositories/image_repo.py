import json
import os
import aiosqlite
from typing import List, Dict, Optional
from pathlib import Path
from nonebot.log import logger

from ..config import plugin_config


class ImageRepository:
    """
    图片元数据仓库
    基于 SQLite (aiosqlite) 查询 pixiv_master_image + pixiv_ai_info，
    结合本地 JSON sidecar 文件解析标签与画师信息。
    """

    def __init__(self):
        self.db_path: str = plugin_config.db_path
        self.image_folder: str = plugin_config.image_folder

    def _get_json_path(self, save_name: str) -> Optional[Path]:
        """从 save_name (如 xxx_p0.jpg) 推导 JSON sidecar 路径 (xxx_p.json)"""
        p = Path(save_name)
        # 11403_p0.jpg -> 11403_p.json
        stem = p.stem  # 11403_p0
        # 去掉最后的 _pN 后缀，换成 _p.json
        if '_p' in stem:
            base = stem.rsplit('_p', 1)[0]
            json_name = f"{base}_p.json"
            return p.parent / json_name
        return None

    def _read_sidecar(self, save_name: str) -> Dict:
        """读取 JSON sidecar 文件，返回 {"tags": [...], "artist": str}"""
        json_path = self._get_json_path(save_name)
        if json_path and json_path.exists():
            try:
                with open(json_path, 'rb') as f:
                    data = json.loads(f.read().decode('utf-8'))
                return {
                    "tags": data.get("Tags", []),
                    "artist": data.get("Artist Name", ""),
                }
            except Exception:
                pass
        return {"tags": [], "artist": ""}

    def _build_info_text(self, title: str, sidecar: Dict, is_ai: bool, image_id: int) -> str:
        """构建图片描述信息"""
        ai_str = "是" if is_ai else "否"
        artist = sidecar.get("artist", "") or "N/A"
        tag_str = ", ".join(str(t) for t in sidecar.get("tags", []))
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
                # 基础 SELECT：LEFT JOIN pixiv_ai_info 以支持 AI 过滤
                sql = """
                    SELECT i.image_id, i.title, i.save_name, a.ai_type
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

                    image_id, title, save_name, ai_type = row

                    # 验证文件实际存在
                    if not save_name or not os.path.exists(save_name):
                        continue

                    is_ai = ai_type is not None and ai_type != 0

                    # 读取 JSON sidecar（获取标签和画师信息）
                    sidecar = self._read_sidecar(save_name)

                    # 分类过滤（R18/R18G 基于标签）
                    if classification:
                        tags_lower = [str(t).lower() for t in sidecar["tags"]]
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
                        tags_joined = " ".join(str(t) for t in sidecar["tags"]).lower()
                        artist_name = sidecar.get("artist", "").lower()
                        searchable = f"{title_lower} {tags_joined} {artist_name}"
                        if not any(kw in searchable for kw in kw_lower):
                            continue

                    info_text = self._build_info_text(title, sidecar, is_ai, image_id)
                    tag_str = ", ".join(str(t) for t in sidecar["tags"])

                    results.append({
                        "path": save_name,
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
                    SELECT i.image_id, i.title, i.save_name, a.ai_type
                    FROM pixiv_master_image i
                    LEFT JOIN pixiv_ai_info a ON i.image_id = a.image_id
                    WHERE i.image_id = ?
                    """,
                    (int(uid),),
                )
                row = await cursor.fetchone()
                if not row:
                    return None

                image_id, title, save_name, ai_type = row

                if not save_name or not os.path.exists(save_name):
                    return None

                is_ai = ai_type is not None and ai_type != 0
                sidecar = self._read_sidecar(save_name)
                info_text = self._build_info_text(title, sidecar, is_ai, image_id)

                return {
                    "path": save_name,
                    "info": info_text,
                    "uid": str(image_id),
                }

        except Exception as e:
            logger.error(f"[ImageRepo] get_image_by_id failed: {e}")
            return None
