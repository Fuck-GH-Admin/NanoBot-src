"""
规则匹配引擎

三个组件：
- RuleProvider:       数据源抽象（协议）
- SQLiteRuleProvider: 基于 RuleRepository 的具体实现
- RuleEngineCore:     纯同步匹配器，无 IO
- RuleEngine:         异步协调器

安全注意：RuleEngine.match 在一次请求中只允许被调用一次。
后续组件必须从 context['_matched_rule'] 读取结果，不能再次调用 match。
"""

import re
from abc import ABC, abstractmethod
from typing import Optional

from ..repositories.rule_repo import RuleRepository

# ── 安全模板 ────────────────────────────────────────────────────
SAFE_PATTERNS: dict[str, re.Pattern] = {
    "JM_ID": re.compile(r"\d{5,}"),
    "MENTION": re.compile(r"\[CQ:at,qq=(\d+)\]"),
    "URL": re.compile(r"https?://\S+"),
}


# ── 数据源抽象 ──────────────────────────────────────────────────
class RuleProvider(ABC):
    @abstractmethod
    async def get_active_rules(self, scope_type: str, scope_id: str) -> list[dict]:
        ...


class SQLiteRuleProvider(RuleProvider):
    def __init__(self, repo: Optional[RuleRepository] = None):
        self._repo = repo or RuleRepository()

    async def get_active_rules(self, scope_type: str, scope_id: str) -> list[dict]:
        return await self._repo.get_active_rules(scope_type, scope_id)


# ── 纯逻辑匹配器（同步） ────────────────────────────────────────
class RuleEngineCore:
    """完全同步，不引入任何 async/await。"""

    @staticmethod
    def match(rules: list[dict], user_msg: str) -> Optional[dict]:
        """
        遍历规则列表，返回最佳匹配或 None。

        流程：
        1. 关键词 AND 过滤
        2. 参数可抽取性校验
        3. 按 priority DESC, confidence DESC, hit_count DESC, 关键词总长度 DESC 排序
        4. 返回第一条
        """
        candidates: list[dict] = []

        for rule in rules:
            keywords: list[str] = rule.get("keywords", [])

            # a. 关键词 AND：所有 keywords 必须都出现在 user_msg 中
            if not all(kw in user_msg for kw in keywords):
                continue

            # b. 参数可抽取性校验
            if not RuleEngineCore._validate_args(rule, user_msg):
                continue

            candidates.append(rule)

        if not candidates:
            return None

        # c. 排序：priority DESC, confidence DESC, hit_count DESC, 关键词总长度 DESC
        candidates.sort(
            key=lambda r: (
                r.get("priority", 0),
                r.get("confidence", 0.0),
                r.get("hit_count", 0),
                sum(len(kw) for kw in r.get("keywords", [])),
            ),
            reverse=True,
        )

        return candidates[0]

    @staticmethod
    def _validate_args(rule: dict, user_msg: str) -> bool:
        """检查是否能从 user_msg 中抽取到规则所需参数。"""
        extractor = rule.get("args_extractor", "none")

        if extractor == "none":
            return True

        if extractor == "number_list":
            return RuleEngineCore._validate_number_list(rule, user_msg)

        if extractor == "string_after_kw":
            return RuleEngineCore._validate_string_after_kw(rule, user_msg)

        if extractor == "pattern":
            return RuleEngineCore._validate_pattern(rule, user_msg)

        return False

    @staticmethod
    def _validate_number_list(rule: dict, user_msg: str) -> bool:
        """找到最后一个命中关键词，窗口：前10字符+后20字符，至少一个数字。"""
        window = RuleEngineCore._get_keyword_window(rule, user_msg, before=10, after=20)
        return bool(re.findall(r"\d+", window))

    @staticmethod
    def _validate_string_after_kw(rule: dict, user_msg: str) -> bool:
        """找到最后一个命中关键词之后文本（最多50字符），去除空白后非空。"""
        pos = RuleEngineCore._find_last_keyword_pos(rule, user_msg)
        if pos == -1:
            return False

        # 找到该关键词的结束位置
        keywords: list[str] = rule.get("keywords", [])
        best_end = -1
        for kw in keywords:
            idx = user_msg.find(kw)
            while idx != -1:
                end = idx + len(kw)
                if end > best_end:
                    best_end = end
                idx = user_msg.find(kw, idx + 1)

        if best_end == -1:
            return False

        after_text = user_msg[best_end:best_end + 50]
        return bool(after_text.strip())

    @staticmethod
    def _validate_pattern(rule: dict, user_msg: str) -> bool:
        """使用 SAFE_PATTERNS[pattern_id] 搜索 user_msg。"""
        pattern_id = rule.get("pattern_id")
        if not pattern_id or pattern_id not in SAFE_PATTERNS:
            return False
        return bool(SAFE_PATTERNS[pattern_id].search(user_msg))

    @staticmethod
    def extract_args(rule: dict, user_msg: str) -> dict:
        """按 args_extractor 类型提取实际参数。"""
        extractor = rule.get("args_extractor", "none")

        if extractor == "none":
            return {}

        if extractor == "number_list":
            return RuleEngineCore._extract_number_list(rule, user_msg)

        if extractor == "string_after_kw":
            return RuleEngineCore._extract_string_after_kw(rule, user_msg)

        if extractor == "pattern":
            return RuleEngineCore._extract_pattern(rule, user_msg)

        return {}

    @staticmethod
    def _extract_number_list(rule: dict, user_msg: str) -> dict:
        window = RuleEngineCore._get_keyword_window(rule, user_msg, before=10, after=20)
        numbers = re.findall(r"\d+", window)
        return {"ids": numbers}

    @staticmethod
    def _extract_string_after_kw(rule: dict, user_msg: str) -> dict:
        keywords: list[str] = rule.get("keywords", [])
        best_end = -1
        for kw in keywords:
            idx = user_msg.find(kw)
            while idx != -1:
                end = idx + len(kw)
                if end > best_end:
                    best_end = end
                idx = user_msg.find(kw, idx + 1)

        if best_end == -1:
            return {"keywords": ""}

        after_text = user_msg[best_end:best_end + 50].strip()
        return {"keywords": after_text}

    @staticmethod
    def _extract_pattern(rule: dict, user_msg: str) -> dict:
        pattern_id = rule.get("pattern_id")
        if not pattern_id or pattern_id not in SAFE_PATTERNS:
            return {}
        m = SAFE_PATTERNS[pattern_id].search(user_msg)
        if m:
            groups = m.groups()
            return {"match": groups[0] if groups else m.group(0)}
        return {}

    # ── 内部工具 ────────────────────────────────────────────────

    @staticmethod
    def _find_last_keyword_pos(rule: dict, user_msg: str) -> int:
        """返回最后一个命中关键词在 user_msg 中的起始位置，未找到返回 -1。"""
        keywords: list[str] = rule.get("keywords", [])
        best = -1
        for kw in keywords:
            idx = user_msg.find(kw)
            while idx != -1:
                if idx > best:
                    best = idx
                idx = user_msg.find(kw, idx + 1)
        return best

    @staticmethod
    def _get_keyword_window(rule: dict, user_msg: str, before: int, after: int) -> str:
        """以最后一个命中关键词为中心，取前 before + 关键词本身 + 后 after 字符。"""
        pos = RuleEngineCore._find_last_keyword_pos(rule, user_msg)
        if pos == -1:
            return ""

        # 找到该位置对应关键词的结束
        keywords: list[str] = rule.get("keywords", [])
        best_end = pos
        for kw in keywords:
            idx = user_msg.find(kw)
            while idx != -1:
                end = idx + len(kw)
                if idx <= pos and end > best_end:
                    best_end = end
                idx = user_msg.find(kw, idx + 1)

        start = max(0, pos - before)
        end = min(len(user_msg), best_end + after)
        return user_msg[start:end]


# ── 异步协调器 ──────────────────────────────────────────────────
class RuleEngine:
    """
    协调器：从 provider 拉取规则，委托 RuleEngineCore 匹配。

    安全注意：match 在一次请求中只允许被调用一次。
    后续组件必须从 context['_matched_rule'] 读取结果。
    """

    def __init__(self, provider: RuleProvider):
        self._provider = provider
        self._core = RuleEngineCore()

    async def match(self, message: str, context: dict) -> Optional[dict]:
        """
        1. 从 context 获取 scope_type, scope_id
        2. 调用 provider.get_active_rules(...)
        3. 调用 RuleEngineCore.match
        4. 结果写入 context['_matched_rule'] 并返回
        """
        scope_type: str = context.get("scope_type", "global")
        scope_id: str = context.get("scope_id", "")

        rules = await self._provider.get_active_rules(scope_type, scope_id)
        result = self._core.match(rules, message)

        if result is not None:
            # 提取参数并附加到结果
            args = self._core.extract_args(result, message)
            result = {**result, "extracted_args": args}

        context["_matched_rule"] = result
        return result
