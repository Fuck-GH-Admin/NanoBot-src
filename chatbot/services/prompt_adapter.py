import html
from pathlib import Path
from typing import Dict, List

from nonebot.log import logger

from ..engine import (
    PromptPipeline, SystemBlock, Priority, ChatMessage, MessageRole,
    to_openai_format, parse_character_card, CharacterCard,
)
from .rule_injector import RuleInjector

_DEFAULT_CHAR_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "config" / "character.json"


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
        """从 memorySnapshot 构建 group_dynamics 和 group_memory 两个 SystemBlock。"""
        blocks: list[SystemBlock] = []

        # Group Dynamics (Priority 4)
        relations = snapshot.get("relations", [])
        if relations:
            rel_xml = "\n".join(
                f"<relation>{self._escape(r['subject_entity'])} "
                f"-{self._escape(r['predicate'])}-> "
                f"{self._escape(r['object_entity'])}</relation>"
                for r in relations
            )
            blocks.append(SystemBlock(
                name="group_dynamics",
                content=f"<group_dynamics>\n{rel_xml}\n</group_dynamics>",
                priority=Priority.GROUP_DYNAMICS,
            ))

        # Group Memory (Priority 5)
        profiles = snapshot.get("profiles", [])
        summary = snapshot.get("summary", "")
        if profiles or summary:
            prof_xml = "\n".join(
                f"<profile uid='{self._escape(p['user_id'])}'>"
                + " ;".join(self._escape(t["content"]) for t in p["traits"])
                + "</profile>"
                for p in profiles
            )
            mem_xml = (
                f"<group_memory>\n"
                f"<summary>{self._escape(summary)}</summary>\n"
                f"{prof_xml}\n"
                f"</group_memory>"
            )
            blocks.append(SystemBlock(
                name="group_memory",
                content=mem_xml,
                priority=Priority.GROUP_MEMORY,
            ))

        return blocks

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
        worldbook_entries: str = "",
    ) -> List[Dict]:
        """
        逻辑脑（调度脑）Prompt 编译。
        极简指令：仅含身份名 + 调度指令，不含角色扮演设定。
        """
        st_history = self._build_st_history(chat_history)

        extra_blocks: list[SystemBlock] = []

        # 极简调度指令
        logic_instruction = (
            f"你是 {self.char_card.name} 的逻辑调度模块。"
            f"分析用户意图，决定是否调用工具、调用哪个工具、传入什么参数。"
            f"重要：仅当用户指令极其明确需要后台操作（如搜图、禁言、下载本子、画图）时才调用工具。"
            f"对于日常问候、情感宣泄、询问观点等纯文本交流，绝对禁止调用任何工具，请直接结束并返回空内容。"
        )
        extra_blocks.append(SystemBlock(
            name="logic_directives",
            content=logic_instruction,
            priority=Priority.SYSTEM_DIRECTIVES,
            never_cut=True,
        ))

        extra_blocks.extend(self._build_extra_blocks_from_snapshot(snapshot))

        rule_block = self._build_rule_instruction_block(context)
        if rule_block:
            extra_blocks.append(rule_block)

        # 世界知识（如有）
        if worldbook_entries:
            extra_blocks.append(SystemBlock(
                name="world_knowledge",
                content=worldbook_entries,
                priority=Priority.WORLD_KNOWLEDGE,
            ))

        final_msgs = self.pipeline.build(
            char=self.char_card,
            user_name="User",
            chat_history=st_history,
            extra_blocks=extra_blocks,
            include_role_play_setting=False,
        )
        return to_openai_format(final_msgs)["messages"]

    def compile_actor_prompt(
        self,
        chat_history: List[Dict],
        snapshot: Dict,
        context: Dict,
        *,
        include_role_play_setting: bool = True,
        system_notification: str = "",
    ) -> List[Dict]:
        """
        演员脑（角色扮演脑）Prompt 编译。
        完整加载：角色卡、世界书、宏替换、角色扮演设定。
        """
        st_history = self._build_st_history(chat_history)

        extra_blocks: list[SystemBlock] = []
        extra_blocks.extend(self._build_extra_blocks_from_snapshot(snapshot))

        # 影子上下文注入
        from .shadow_context import ShadowContext
        session_id = f"group_{context.get('group_id', 0)}"
        shadow_facts = ShadowContext().get_recent(session_id)
        if shadow_facts:
            shadow_text = "\n".join(f"- {s}" for s in shadow_facts)
            extra_blocks.append(SystemBlock(
                name="shadow_context",
                content=f"<system_shadow_context>\n以下内容仅供你理解当前状态，不代表用户发言：\n{shadow_text}\n</system_shadow_context>",
                priority=Priority.SYSTEM_DIRECTIVES,
                never_cut=True,
            ))

        rule_block = self._build_rule_instruction_block(context)
        if rule_block:
            extra_blocks.append(rule_block)

        # 系统通知：以 SystemBlock 注入（不再伪装为 USER 消息）
        if system_notification:
            logger.info(f"[AUDIT_SYSTEM_INJECT] 正在向演员脑注入系统客观状态: {system_notification[:100]}...")
            if not system_notification.startswith("[SYSTEM_TOOL_RESULT]"):
                system_notification = f"[SYSTEM_TOOL_RESULT] {system_notification}"
            full_text = (
                f"[全局最新状态更新] {system_notification}\n"
                f"(以上是刚刚由系统后台动作完成的最新结果，请结合对话历史最后一条，用自然语言向用户转述。)"
            )
            extra_blocks.append(SystemBlock(
                name="system_tool_result",
                content=full_text,
                priority=Priority.SYSTEM_DIRECTIVES,
                never_cut=True,
            ))

        final_msgs = self.pipeline.build(
            char=self.char_card,
            user_name="User",
            chat_history=st_history,
            extra_blocks=extra_blocks,
            include_role_play_setting=include_role_play_setting,
        )
        return to_openai_format(final_msgs)["messages"]
