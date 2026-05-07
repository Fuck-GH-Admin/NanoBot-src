"""
RuleInjector — 将匹配到的规则转换为 LLM 可理解的 system 指令。

纯逻辑，无 IO。
"""

from typing import Any, Dict, List


# 截断常量
_MAX_DESCRIPTION_LEN = 100
_MAX_EXAMPLES = 2
_MAX_EXAMPLE_TOTAL_LEN = 120


class RuleInjector:
    """将单条匹配规则构建为 system 指令文本。"""

    @staticmethod
    def build_instruction(rule: Dict[str, Any]) -> str:
        """
        生成 system 指令文本，包含：
        - 触发关键词
        - 目标工具名
        - 参数提取方式
        - 1~2 个示例
        - 行为边界警告

        长度硬截断：
        - description → 100 字符
        - examples → 最多 2 条
        - 每条 example 的 input+call → 120 字符
        """
        keywords: List[str] = rule.get("keywords", [])
        tool_name: str = rule.get("tool_name", "")
        args_extractor: str = rule.get("args_extractor", "none")
        description: str = rule.get("description") or ""
        examples: List[Dict[str, str]] = rule.get("examples") or []

        # 截断 description
        if len(description) > _MAX_DESCRIPTION_LEN:
            description = description[:_MAX_DESCRIPTION_LEN] + "..."

        # 截断 examples
        examples = examples[:_MAX_EXAMPLES]
        trimmed_examples = []
        for ex in examples:
            inp = str(ex.get("input", ""))
            call = str(ex.get("call", ""))
            total = f"输入: {inp} → 调用: {call}"
            if len(total) > _MAX_EXAMPLE_TOTAL_LEN:
                # 按比例截断
                budget = _MAX_EXAMPLE_TOTAL_LEN - len("输入:  → 调用: ")
                half = budget // 2
                if len(inp) > half:
                    inp = inp[:half] + "..."
                remaining = budget - len(inp)
                if len(call) > remaining:
                    call = call[:remaining] + "..."
                total = f"输入: {inp} → 调用: {call}"
            trimmed_examples.append(total)

        # 组装指令
        parts = [
            "=== 动态规则指令 ===",
            f"触发关键词: {', '.join(keywords)}",
            f"目标工具: {tool_name}",
            f"参数提取方式: {args_extractor}",
        ]

        if description:
            parts.append(f"规则说明: {description}")

        if trimmed_examples:
            parts.append("示例:")
            for i, ex in enumerate(trimmed_examples, 1):
                parts.append(f"  {i}. {ex}")

        parts.append(
            "\n⚠️ 行为边界：仅执行本规则要求的动作，禁止过度脑补或混合其他规则。"
            "不要假设未明确说明的参数或意图。"
        )

        return "\n".join(parts)
