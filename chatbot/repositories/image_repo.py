import os
import pandas as pd
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from nonebot.log import logger

from ..config import plugin_config

class ImageRepository:
    """
    图片元数据仓库
    基于 Pandas 提供高效的 Excel 数据查询和文件路径管理。
    """
    def __init__(self):
        self.excel_path = plugin_config.excel_path
        self.image_folder = plugin_config.image_folder
        self.df: Optional[pd.DataFrame] = None
        self._load_data()

    def _load_data(self):
        """同步加载 Excel 数据（通常在启动时执行）"""
        if not os.path.exists(self.excel_path):
            logger.warning(f"[ImageRepo] Excel file not found: {self.excel_path}")
            return

        try:
            self.df = pd.read_excel(self.excel_path)
            logger.info(f"[ImageRepo] Loaded {len(self.df)} image records.")
        except Exception as e:
            logger.error(f"[ImageRepo] Failed to load Excel: {e}")
            self.df = None

    def refresh(self):
        """手动刷新数据"""
        self._load_data()

    def query_images(self, 
                    keywords: List[str], 
                    classification: Optional[str] = None, 
                    no_ai: bool = False,
                    limit: int = 50) -> List[Dict[str, str]]:
        """
        查询图片
        :param keywords: 关键词列表 (OR 逻辑，匹配任意一个即可，但会根据匹配度排序)
        :param classification: 分类筛选 (R18, R18G, Artist)
        :param no_ai: 是否过滤 AI 作品
        :param limit: 最大返回数量
        :return: 图片信息列表 [{"path": str, "info": str, "id": str, ...}]
        """
        if self.df is None or self.df.empty:
            logger.warning("[ImageRepo] Dataframe is empty or not loaded.")
            return []

        df_view = self.df.copy()

        # 1. 分类过滤
        if classification:
            df_view = df_view[df_view['分类'] == classification]
            if df_view.empty:
                return []

        # 2. AI 过滤
        if no_ai:
            # 假设 Excel 中 '是否AI' 列为 1/0 或 True/False
            df_view = df_view[~df_view['是否AI'].astype(bool)]
            if df_view.empty:
                return []

        # 3. 关键词过滤
        if keywords:
            # 获取所有文本类型的列（排除目录列）
            text_cols = [c for c in df_view.select_dtypes(include=['object']).columns if c != '相对目录']
            
            # 创建一个用于计分的 Series
            df_view['match_score'] = 0
            
            for kw in keywords:
                kw_str = str(kw).lower()
                # 在所有文本列中查找
                row_mask = pd.Series([False] * len(df_view), index=df_view.index)
                for col in text_cols:
                    # 包含关键词则为 True
                    col_contains = df_view[col].astype(str).str.lower().str.contains(kw_str, regex=False, na=False)
                    row_mask |= col_contains
                
                # 匹配到的行分数 +1
                df_view.loc[row_mask, 'match_score'] += 1
            
            # 过滤掉分数为 0 的行
            df_view = df_view[df_view['match_score'] > 0]
            
            # 按匹配分数降序排序
            df_view = df_view.sort_values(by='match_score', ascending=False)

        # 4. 随机抽样或取前N个
        # 如果没有关键词，随机打乱；如果有关键词，前面已经排好序了
        if not keywords:
            df_view = df_view.sample(frac=1) # Shuffle

        results = []
        count = 0
        
        # 遍历结果并验证文件存在性
        for _, row in df_view.iterrows():
            if count >= limit:
                break
                
            rel_path = str(row.get('相对目录', '')).replace('\\', os.sep)
            full_path = os.path.join(self.image_folder, rel_path)
            
            if os.path.exists(full_path):
                # 构建返回信息
                is_ai = '是' if bool(row.get('是否AI')) else '否'
                info_text = (
                    f"画师: {row.get('画师名称', 'N/A')} (ID: {row.get('画师UID', 'N/A')})\n"
                    f"图片ID: {row.get('图片文件UID', 'N/A')}\n"
                    f"标题: {row.get('作品标题', 'N/A')}\n"
                    f"标签: {row.get('图片标签', 'N/A')}\n"
                    f"分类: {row.get('分类', 'N/A')} | AI: {is_ai}"
                )
                
                results.append({
                    "path": full_path,
                    "info": info_text,
                    "uid": str(row.get('图片文件UID', '')),
                    "tags": str(row.get('图片标签', ''))
                })
                count += 1
            else:
                logger.debug(f"[ImageRepo] File missing: {full_path}")

        return results

    def get_image_by_id(self, uid: str) -> Optional[Dict]:
        """根据图片UID精确获取图片"""
        if self.df is None: return None
        
        # 转换为字符串比较
        row = self.df[self.df['图片文件UID'].astype(str) == str(uid)]
        if row.empty:
            return None
            
        row = row.iloc[0]
        rel_path = str(row.get('相对目录', '')).replace('\\', os.sep)
        full_path = os.path.join(self.image_folder, rel_path)
        
        if os.path.exists(full_path):
            is_ai = '是' if bool(row.get('是否AI')) else '否'
            info_text = (
                f"画师: {row.get('画师名称', 'N/A')}\n"
                f"ID: {row.get('图片文件UID', 'N/A')}\n"
                f"AI: {is_ai}"
            )
            return {
                "path": full_path,
                "info": info_text,
                "uid": str(row.get('图片文件UID', ''))
            }
        return None