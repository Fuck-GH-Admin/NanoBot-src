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
        worldbook_entries: str = "",
    ) -> List[Dict]:
        """
        逻辑脑（调度脑）Prompt 编译。
        极简指令：仅含身份名 + 调度指令，不含角色扮演设定。
        """
        st_history = self._build_st_history(chat_history)

        extra_blocks: list[SystemBlock] = []

        # 极简调度指令：应用 Anthropic 防御性提示词范式
        logic_instruction = (
            f"=== CRITICAL: PURE LOGIC SCHEDULER MODE ===\n"
            f"你是 {self.char_card.name} 的底层逻辑调度模块。你的唯一使命是：分析用户意图并调用适当的工具。\n\n"
            f"【STRICTLY PROHIBITED ACTIONS】\n"
            f"你被严厉禁止执行以下操作（尝试执行将导致系统崩溃）：\n"
            f"- 禁止输出任何自然语言、对话、解释或角色扮演。\n"
            f"- 禁止在工具调用前添加任何思考过程（如“我判断用户需要...”）。\n"
            f"- 严禁主动结束用户的会话状态。结束会话由下游机制判断。\n\n"
            f"【YOUR ONLY ALLOWED BEHAVIOR】\n"
            f"1. 如果用户指令明确需要后台操作（如搜图、系统管理），调用对应功能工具。\n"
            f"2. 如果用户输入为日常问候、聊天、无意义字符或纯情绪宣泄，你必须调用 `no_op` 工具。\n"
            f"你的每轮输出必须有且仅有一个有效的工具调用（JSON格式），`content` 字段必须永久保持为空。"
        )
        extra_blocks.append(SystemBlock(
            name="logic_directives",
            content=logic_instruction,
            priority=Priority.SYSTEM_DIRECTIVES,
            never_cut=True,
        ))

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

        # 防线 2：末尾 System 警告 — 使用 Anthropic 风格的强制收口
        st_history.append(ChatMessage(
            role=MessageRole.SYSTEM,
            content=(
                "=== CRITICAL REMINDER ===\n"
                "Your turn should ONLY end by calling a tool. \n"
                "If it's a specific task, call the relevant tool. If it's pure chat, call `no_op`. \n"
                "DO NOT output any conversational text. Keep `content` entirely empty."
            ),
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
        worldbook_entries: str = "",
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

        # 会话生命周期感知指令
        extra_blocks.append(SystemBlock(
            name="session_lifecycle",
            content=(
                "【会话生命周期感知】"
                "你具备感知对话是否自然结束的能力。"
                "当你判断本次对话已进入尾声（如：用户已得到满意答复、对话自然结束、用户表达了告别），"
                "请在回复末尾附加如下控制代码块（用户不可见）：\n"
                "```session_ctl\n{\"close_session\": true}\n```\n"
                "仅在你确信对话已结束时使用此标志。正常对话中不要附加。"
            ),
            priority=Priority.SYSTEM_DIRECTIVES,
            never_cut=True,
        ))

        # 世界观注入（仅演员脑）
        if worldbook_entries:
            extra_blocks.append(SystemBlock(
                name="actor_world_knowledge",
                content=f"[世界观与客观环境设定]\n{worldbook_entries}",
                priority=Priority.WORLD_KNOWLEDGE,
            ))

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
