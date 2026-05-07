# src/plugins/chatbot/tools/agent_tools/rule_tool.py

from typing import Any, Dict, List, Tuple

from nonebot.log import logger

from ..base_tool import BaseTool
from ...utils.keyword_utils import normalize_keywords, compute_keywords_hash
from ...repositories.rule_repo import RuleRepository

# ── 可学习工具 & 高风险黑名单 ─────────────────────────────────
LEARNABLE_TOOLS = ["jm_download", "search_acg_image", "recommend_book", "generate_image"]
DANGEROUS_TOOLS = {"ban_user"}

# ── 停用词 ────────────────────────────────────────────────────
STOP_WORDS = frozenset({
    # 中文高频虚词
    "我", "的", "了", "是", "在", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有",
    "看", "好", "自己", "这", "他", "她", "它", "吗", "吧", "啊", "呢", "嗯",
    "请", "帮", "帮忙", "可以", "能", "想", "给", "对", "把", "被", "让",
    "那", "这个", "那个", "什么", "怎么", "怎样", "如何", "多少", "几",
    "么", "呀", "哦", "哈", "嘿", "喂", "嗨",
    # 英文高频停用词
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "i", "you", "he", "she", "it",
    "we", "they", "me", "him", "her", "us", "them", "my", "your", "his",
    "its", "our", "their", "this", "that", "these", "those", "and", "or",
    "but", "if", "then", "so", "for", "of", "to", "in", "on", "at", "by",
    "with", "from", "as", "into", "about", "please", "help", "want",
})


def _filter_stop_words(keywords: list[str]) -> list[str]:
    """从已规范化的关键词列表中剔除停用词。"""
    return [kw for kw in keywords if kw not in STOP_WORDS]


class LearnRuleTool(BaseTool):
    """管理员教学工具：创建或覆盖动态暗号规则。"""

    name = "learn_rule"
    is_write_operation = True
    description = (
        "教我一个新的暗号规则。当用户说出特定关键词时，我会自动调用对应工具。"
        "仅管理员可用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "触发关键词列表，2-3 个，如 ['jm', '下载']",
            },
            "tool_name": {
                "type": "string",
                "description": f"要绑定的工具名，可选: {', '.join(LEARNABLE_TOOLS)}",
                "enum": LEARNABLE_TOOLS,
            },
            "args_extractor": {
                "type": "string",
                "description": "参数提取方式",
                "enum": ["number_list", "string_after_kw", "none", "pattern"],
            },
            "pattern_id": {
                "type": "string",
                "description": "当 args_extractor 为 pattern 时必填",
                "enum": ["JM_ID", "MENTION", "URL"],
            },
            "description": {
                "type": "string",
                "description": "规则用途说明（可选）",
            },
            "examples": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string"},
                        "call": {"type": "string"},
                    },
                },
                "description": "示例（可选）",
            },
            "scope_type": {
                "type": "string",
                "description": "作用域类型（可选，默认根据上下文推断）",
                "enum": ["group", "private", "global"],
            },
            "scope_id": {
                "type": "string",
                "description": "作用域 ID（可选）",
            },
            "force_overwrite": {
                "type": "boolean",
                "description": "冲突时是否强制覆盖已有规则，默认 false",
            },
        },
        "required": ["keywords", "tool_name", "args_extractor"],
    }
    require_permission = "admin"

    async def execute(
        self, arguments: Dict[str, Any], context: Dict[str, Any]
    ) -> Tuple[str, List[str]]:
        # 1. 校验 tool_name
        tool_name: str = arguments.get("tool_name", "")
        if tool_name in DANGEROUS_TOOLS:
            return f"错误：工具 '{tool_name}' 属于高风险操作，不允许学习。", []
        if tool_name not in LEARNABLE_TOOLS:
            return (
                f"错误：工具 '{tool_name}' 不在可学习列表中。"
                f"可选: {', '.join(LEARNABLE_TOOLS)}",
                [],
            )

        # 2. 校验 & 规范化 keywords
        raw_keywords: list[str] = arguments.get("keywords", [])
        if not raw_keywords or not isinstance(raw_keywords, list):
            return "错误：keywords 不能为空。", []

        normalized = normalize_keywords(raw_keywords)
        filtered = _filter_stop_words(normalized)
        if not filtered:
            return "错误：过滤停用词后关键词为空，请提供更有意义的关键词。", []

        # 3. args_extractor 校验
        args_extractor: str = arguments.get("args_extractor", "none")
        pattern_id = arguments.get("pattern_id")
        if args_extractor == "pattern" and not pattern_id:
            return "错误：args_extractor 为 'pattern' 时必须提供 pattern_id。", []

        # 4. 计算 hash & 确定作用域
        keywords_hash = compute_keywords_hash(filtered)
        scope_type: str = arguments.get("scope_type") or context.get("scope_type", "global")
        scope_id: str = arguments.get("scope_id") or context.get("scope_id", "")
        operator: str = context.get("user_id", "system")
        force_overwrite: bool = arguments.get("force_overwrite", False)

        repo = RuleRepository()

        # 5. 冲突检测
        existing = await repo.find_by_hash(scope_type, scope_id, keywords_hash)

        if existing and not force_overwrite:
            return (
                f"规则冲突！已存在相同关键词的规则：\n"
                f"- rule_id: {existing['rule_id']}\n"
                f"- 工具: {existing['tool_name']}\n"
                f"- 创建者: {existing['created_by']}\n"
                f"- 描述: {existing.get('description') or '无'}\n\n"
                f"如确认覆盖，请将 force_overwrite 设为 true 再次调用。",
                [],
            )

        # 6. 构造规则数据
        rule_data = {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "keywords": filtered,
            "keywords_hash": keywords_hash,
            "tool_name": tool_name,
            "args_extractor": args_extractor,
            "pattern_id": pattern_id,
            "description": arguments.get("description"),
            "examples": arguments.get("examples"),
            "created_by": operator,
        }

        if existing and force_overwrite:
            # 覆盖更新
            await repo.update_rule(existing["rule_id"], {**rule_data, "operator": operator})
            logger.info(
                f"[LearnRuleTool] 规则已覆盖: {existing['rule_id']} "
                f"by {operator} keywords={filtered}"
            )
            return (
                f"规则已覆盖更新！\n"
                f"- rule_id: {existing['rule_id']}\n"
                f"- 关键词: {filtered}\n"
                f"- 工具: {tool_name}",
                [],
            )
        else:
            # 新建规则
            created = await repo.create_rule(rule_data)
            logger.info(
                f"[LearnRuleTool] 新规则已创建: {created['rule_id']} "
                f"by {operator} keywords={filtered}"
            )
            return (
                f"新规则已创建！\n"
                f"- rule_id: {created['rule_id']}\n"
                f"- 关键词: {filtered}\n"
                f"- 工具: {tool_name}\n"
                f"- 提取方式: {args_extractor}",
                [],
            )


class ForgetRuleTool(BaseTool):
    """管理员遗忘工具：删除已有规则。"""

    name = "forget_rule"
    is_write_operation = True
    description = "删除一条已有的暗号规则。仅管理员可用。"
    parameters = {
        "type": "object",
        "properties": {
            "rule_id_or_keyword": {
                "type": "string",
                "description": "要删除的规则 ID，或关键词（模糊匹配同作用域内的规则）",
            },
        },
        "required": ["rule_id_or_keyword"],
    }
    require_permission = "admin"

    async def execute(
        self, arguments: Dict[str, Any], context: Dict[str, Any]
    ) -> Tuple[str, List[str]]:
        query: str = arguments.get("rule_id_or_keyword", "").strip()
        if not query:
            return "错误：请提供 rule_id 或关键词。", []

        operator: str = context.get("user_id", "system")
        scope_type: str = context.get("scope_type", "global")
        scope_id: str = context.get("scope_id", "")

        repo = RuleRepository()

        # 尝试按 rule_id 精确删除
        deleted = await repo.delete_rule(query, operator=operator)
        if deleted:
            logger.info(f"[ForgetRuleTool] 按 rule_id 删除: {query} by {operator}")
            return f"规则 {query} 已删除。", []

        # 按关键词在同作用域内模糊匹配
        active_rules = await repo.get_active_rules(scope_type, scope_id)
        matched_ids = []
        for rule in active_rules:
            if query in rule.get("keywords", []):
                matched_ids.append(rule["rule_id"])

        if not matched_ids:
            return f"未找到与 '{query}' 匹配的规则。", []

        deleted_count = 0
        for rid in matched_ids:
            ok = await repo.delete_rule(rid, operator=operator)
            if ok:
                deleted_count += 1

        logger.info(
            f"[ForgetRuleTool] 按关键词 '{query}' 删除 {deleted_count} 条规则 by {operator}"
        )
        return f"已删除 {deleted_count} 条包含关键词 '{query}' 的规则。", []
