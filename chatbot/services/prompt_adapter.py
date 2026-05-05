import html
import json
from pathlib import Path
from typing import Dict, Any, List

from ..engine import (
    PromptPipeline, SystemBlock, Priority, ChatMessage, MessageRole,
    to_openai_format, parse_character_card, CharacterCard,
)

_DEFAULT_CHAR_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "config" / "character.json"


class PromptAdapter:
    def __init__(self, max_tokens: int = 4000):
        self.pipeline = PromptPipeline(max_tokens=max_tokens)
        self.char_card = self._load_character_card()

    def _load_character_card(self) -> CharacterCard:
        if _DEFAULT_CHAR_PATH.exists():
            with open(_DEFAULT_CHAR_PATH, "r", encoding="utf-8") as f:
                return parse_character_card(f.read(), format="json")
        return CharacterCard(name="Assistant", description="A helpful assistant.")

    @staticmethod
    def _escape(text: str) -> str:
        return html.escape(str(text), quote=False)

    def compile_prompt(
        self,
        chat_history: List[Dict],
        snapshot: Dict,
        context: Dict,
    ) -> List[Dict]:
        st_history = [
            ChatMessage(
                role=MessageRole(msg["role"]),
                content=msg.get("content", ""),
                name=msg.get("name", ""),
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
            )
            for msg in chat_history
        ]

        extra_blocks: list[SystemBlock] = []

        # Group Dynamics (Priority 4)
        relations = snapshot.get("relations", [])
        if relations:
            rel_xml = "\n".join(
                f"<relation>{self._escape(r['subject_entity'])} "
                f"-{self._escape(r['predicate'])}-> "
                f"{self._escape(r['object_entity'])}</relation>"
                for r in relations
            )
            extra_blocks.append(SystemBlock(
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
            extra_blocks.append(SystemBlock(
                name="group_memory",
                content=mem_xml,
                priority=Priority.GROUP_MEMORY,
            ))

        final_msgs = self.pipeline.build(
            char=self.char_card,
            user_name="User",
            chat_history=st_history,
            extra_blocks=extra_blocks,
        )
        return to_openai_format(final_msgs)["messages"]
