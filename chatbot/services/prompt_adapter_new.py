import html
from pathlib import Path
from typing import Dict, List

from nonebot.log import logger

from ..engine import (
    PromptPipeline, SystemBlock, Priority, ChatMessage, MessageRole,
    to_openai_format, parse_character_card, CharacterCard,
)
from .rule_injector import RuleInjector

from ..utils.path_utils import CHARACTER_PATH
_DEFAULT_CHAR_PATH = CHARACTER_PATH


class PromptAdapter:
    def __init__(self, max_tokens: int = 4000):
        self.pipeline = PromptPipeline(max_tokens=max_tokens)
        self.char_card = self._load_character_card()
        self.rule_injector = RuleInjector()

    def _load_character_card(self) -> CharacterCard:
        if _DEFAULT_CHAR_PATH.exists():
            with open(_DEFAULT_CHAR_PATH, "r", encoding="utf-8") as f:
                return parse_character_card(f.read(), format="json")
        return CharacterCard(name="Assistant", description="A helpful assistant.")

    @staticmethod
    def _escape(text: str) -> str:
        return html.escape(str(text), quote=False)

    def _build_extra_blocks_from_snapshot(self, snapshot: Dict) -> list[SystemBlock]:
        """从 memorySnapshot 构建 SystemBlock（已废弃旧表注入）。

        旧的 group_dynamics (Relation) 和 group_memory (UserTrait/GroupMemory) 注入
        已停止。这些数据不再写入（压缩机制已移除），残余数据不再塞进 Actor Prompt。
        未来人物关系将合并到世界书 JSON 格式中。
        """
        return []

    def _build_rule_instruction_block(self, context: Dict) -> SystemBlock | None:
        """从 context 中读取已匹配的动态规则，构建规则指令 SystemBlock。"""
        matched_rule = context.get("_matched_rule")
        if not matched_rule:
            return None
        instruction = self.rule_injector.build_instruction(matched_rule)
        return SystemBlock(
            name="dynamic_rule",
            content=instruction,
            priority=Priority.SYSTEM_DIRECTIVES,
            never_cut=True,
        )

    def _build_st_history(self, chat_history: List[Dict]) -> List[ChatMessage]:
        """将原始聊天历史转为 ChatMessage 列表，USER 消息注入身份锚点。"""
        st_history = []
        for msg in chat_history:
            role = MessageRole(msg["role"])
            content = msg.get("content", "")

            if role == MessageRole.USER:
                uid = msg.get("user_id", "")
                name = msg.get("name", "Unknown")
                if uid:
                    content = f"[ID:{uid}] {name}：{content}"

            st_history.append(ChatMessage(
                role=role,
                content=content,
                name=msg.get("name", ""),
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
            ))
        return st_history

    def compile_logic_prompt(
        self,
        chat_history: List[Dict],
        snapshot: Dict,
        context: Dict,
        tools: List[Dict],
        worldbook_entries: str = "",
    ) -> List[Dict]:
        """
        逻辑脑（调度脑）Prompt 编译。
        采用类 Claude Code 的 XML CoT 协议，强制隔离思考过程与工具执行。
        """
        st_history = self._build_st_history(chat_history)

        extra_blocks: list[SystemBlock] = []

        # 1. 动态生成 XML 工具字典
        tools_xml_parts = []
        for t in tools:
            f = t.get("function", {})
            props = f.get("parameters", {}).get("properties", {})
            req = f.get("parameters", {}).get("required", [])

            param_xml = []
            for p_name, p_info in props.items():
                req_str = ' required="true"' if p_name in req else ""
                param_xml.append(
                    f'      <param name="{p_name}" type="{p_info.get("type", "string")}"{req_str}>'
                    f'{p_info.get("description", "")}</param>'
                )

            param_block = ""
            if param_xml:
                param_block = "\n    <parameters>\n" + "\n".join(param_xml) + "\n    </parameters>"

            tool_desc = (
                "  <tool>\n"
                f'    <name>{f.get("name")}</name>\n'
                f'    <description>{f.get("description")}</description>{param_block}\n'
                "  </tool>"
            )
            tools_xml_parts.append(tool_desc)

        available_tools_xml = "<available_tools>\n" + "\n".join(tools_xml_parts) + "\n</available_tools>"

        # 2. 极简调度指令：XML CoT 范式
        logic_instruction = (
            "=== CRITICAL: PURE LOGIC SCHEDULER MODE ===\n"
            f"你是 {self.char_card.name} 的底层逻辑调度模块。你的唯一使命是：分析用户意图并调用适当的工具。\n\n"
            f"{available_tools_xml}\n\n"
            "<execution_rules>\n"
            "1. 你必须先在 <thinking> 标签内进行意图分析和参数推导。\n"
            "2. 思考完成后，必须且只能输出一个 <invoke> 标签来调用工具。\n"
            "3. 绝对禁止输出任何针对用户的自然语言对话。\n"
            "4. 如果判断为日常闲聊，必须调用 `no_op` 工具，将控制权交给人格脑。\n"
            "</execution_rules>\n\n"
            "<example>\n"
            "<thinking>用户要求看一张图，不需要特定关键词，因此调用 search_acg_image。</thinking>\n"
            '<invoke name="search_acg_image">\n'
