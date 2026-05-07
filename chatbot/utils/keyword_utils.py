"""关键词规范化工具 — 统一入口，禁止其他地方手写哈希逻辑。"""

import hashlib


def normalize_keywords(keywords: list[str]) -> list[str]:
    """对关键词去重、去空、trim、lower、排序。"""
    seen: set[str] = set()
    result: list[str] = []
    for kw in keywords:
        kw = kw.strip().lower()
        if kw and kw not in seen:
            seen.add(kw)
            result.append(kw)
    result.sort()
    return result


def compute_keywords_hash(keywords: list[str]) -> str:
    """规范化后拼接并取 MD5 hex。"""
    normalized = normalize_keywords(keywords)
    joined = "_".join(normalized)
    return hashlib.md5(joined.encode("utf-8")).hexdigest()
