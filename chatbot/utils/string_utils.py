import re
from typing import Set

class StringUtils:
    """
    字符串处理工具类
    提供模糊匹配、编辑距离计算、文本清洗等功能。
    """

    @staticmethod
    def clean_text(text: str) -> str:
        """
        清洗文本，仅保留中文、字母、数字和下划线。
        用于去除标点符号干扰，提高匹配准确率。
        """
        if not text:
            return ""
        return re.sub(r'[^\w\u4e00-\u9fff]', '', text)

    @staticmethod
    def levenshtein_distance(s1: str, s2: str) -> int:
        """
        计算两个字符串的莱文斯坦编辑距离 (DP算法)
        """
        if len(s1) < len(s2):
            return StringUtils.levenshtein_distance(s2, s1)

        if len(s2) == 0:
            return len(s1)

        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        
        return previous_row[-1]

    @staticmethod
    def fuzzy_match(text: str, keyword: str, threshold: int = 2) -> bool:
        """
        模糊匹配判断
        1. 如果 keyword 包含在 text 中，返回 True
        2. 如果清洗后的文本包含清洗后的 keyword，返回 True
        3. 如果编辑距离小于等于阈值，返回 True
        
        :param text: 用户输入的文本
        :param keyword: 目标关键词
        :param threshold: 允许的最大编辑距离
        """
        if not text or not keyword:
            return False

        # 1. 包含匹配
        if keyword in text:
            return True
            
        # 2. 清洗后匹配
        clean_text = StringUtils.clean_text(text)
        clean_keyword = StringUtils.clean_text(keyword)
        
        if clean_keyword in clean_text:
            return True
            
        # 3. 编辑距离匹配（仅当文本长度接近时才计算，优化性能）
        if abs(len(clean_text) - len(clean_keyword)) > threshold:
            return False
            
        return StringUtils.levenshtein_distance(clean_text, clean_keyword) <= threshold

    @staticmethod
    def contains_all_chars(text: str, keyword: str) -> bool:
        """
        检查 text 是否包含 keyword 中的所有字符（顺序不限）
        """
        return set(keyword).issubset(set(text))