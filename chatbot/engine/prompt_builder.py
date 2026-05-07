"""
SillyTavern Prompt Builder.

Provides two prompt assembly paths mirroring the original JS implementation:
- Story String path (text completion APIs): Handlebars/Jinja2 template rendering
- Chat Completion path (OpenAI-style APIs): structured message array assembly
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable, Optional

from jinja2 import BaseLoader, Environment, TemplateError, Undefined

from .card_schema import CharacterCard
from .macro_engine import MacroEngine


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"  # 新增此行以支持工具调用回调消息


class InjectionPosition(IntEnum):
    RELATIVE = 0   # Injected relative to other prompts in system area
    ABSOLUTE = 1   # Injected at a specific depth in chat history


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PromptEntry:
    """A single prompt entry in the prompt collection."""
    identifier: str
    content: str
    role: MessageRole = MessageRole.SYSTEM
    injection_position: InjectionPosition = InjectionPosition.RELATIVE
    injection_depth: int = 0
    injection_order: int = 100
    enabled: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.role, str):
            self.role = MessageRole(self.role)
        if isinstance(self.injection_position, int):
            self.injection_position = InjectionPosition(self.injection_position)


@dataclass
class ChatMessage:
    """A chat message for the final output."""
    role: MessageRole
    content: str
    name: str = ""
    injected: bool = False  # True if this message was depth-injected
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.role, str):
            self.role = MessageRole(self.role)


@dataclass
class PromptCollection:
    """An ordered collection of prompt entries."""
    entries: list[PromptEntry] = field(default_factory=list)

    def add(self, entry: PromptEntry) -> None:
        # Replace existing entry with same identifier
        for i, e in enumerate(self.entries):
            if e.identifier == entry.identifier:
                self.entries[i] = entry
                return
        self.entries.append(entry)

    def get(self, identifier: str) -> PromptEntry | None:
        for e in self.entries:
            if e.identifier == identifier:
                return e
        return None

    def remove(self, identifier: str) -> None:
        self.entries = [e for e in self.entries if e.identifier != identifier]

    def enabled_entries(self) -> list[PromptEntry]:
        return [e for e in self.entries if e.enabled]

    def __len__(self) -> int:
        return len(self.entries)


# ---------------------------------------------------------------------------
# Default prompt templates
# ---------------------------------------------------------------------------

# Default story string template (Jinja2, mimicking Handlebars)
DEFAULT_STORY_STRING_TEMPLATE = (
    "{% if system %}{{ system }}\n{% endif %}"
    "{% if description %}{{ description }}\n{% endif %}"
    "{% if personality %}{{ char }}'s personality: {{ personality }}\n{% endif %}"
    "{% if scenario %}Scenario: {{ scenario }}\n{% endif %}"
    "{% if persona %}{{ persona }}\n{% endif %}"
)

# Full default template with world info
FULL_STORY_STRING_TEMPLATE = (
    "{% if anchor_before %}{{ anchor_before }}\n{% endif %}"
    "{% if system %}{{ system }}\n{% endif %}"
    "{% if wi_before %}{{ wi_before }}\n{% endif %}"
    "{% if description %}{{ description }}\n{% endif %}"
    "{% if personality %}{{ personality }}\n{% endif %}"
    "{% if scenario %}Scenario: {{ scenario }}\n{% endif %}"
    "{% if wi_after %}{{ wi_after }}\n{% endif %}"
    "{% if persona %}{{ persona }}\n{% endif %}"
    "{% if anchor_after %}{{ anchor_after }}\n{% endif %}"
)


# ---------------------------------------------------------------------------
# Default Chat Completion prompt order
# ---------------------------------------------------------------------------

@dataclass
class DefaultPromptConfig:
    """Default configuration for a Chat Completion prompt."""
    identifier: str
    content: str
    role: MessageRole = MessageRole.SYSTEM
    enabled: bool = True


DEFAULT_CHAT_COMPLETION_PROMPTS: list[DefaultPromptConfig] = [
    DefaultPromptConfig("main", "Write {{char}}'s next reply in a fictional chat between {{charIfNotGroup}} and {{user}}."),
    DefaultPromptConfig("worldInfoBefore", "", MessageRole.SYSTEM),
    DefaultPromptConfig("personaDescription", "", MessageRole.SYSTEM),
    DefaultPromptConfig("charDescription", "", MessageRole.SYSTEM),
    DefaultPromptConfig("charPersonality", "", MessageRole.SYSTEM),
    DefaultPromptConfig("scenario", "", MessageRole.SYSTEM),
    DefaultPromptConfig("enhanceDefinitions", "If you have more knowledge of {{char}}, add to the character's lore and personality to enhance them but keep the Character Sheet's definitions absolute.", enabled=False),
    DefaultPromptConfig("nsfw", "", MessageRole.SYSTEM),
    DefaultPromptConfig("worldInfoAfter", "", MessageRole.SYSTEM),
    DefaultPromptConfig("dialogueExamples", "", MessageRole.SYSTEM),
    DefaultPromptConfig("chatHistory", "", MessageRole.SYSTEM),
    DefaultPromptConfig("jailbreak", "", MessageRole.SYSTEM),
]


# ---------------------------------------------------------------------------
# Story String Builder (Text Completion path)
# ---------------------------------------------------------------------------

class StoryStringBuilder:
    """
    Renders a Jinja2 story string template with character card data.

    This mirrors SillyTavern's renderStoryString() from power-user.js.
    """

    def __init__(
        self,
        template: str = DEFAULT_STORY_STRING_TEMPLATE,
        macro_engine: MacroEngine | None = None,
    ) -> None:
        self._template_str = template
        self._env = Environment(
            loader=BaseLoader(),
            autoescape=False,
            undefined=_LenientUndefined,
        )
        self._macro_engine = macro_engine or MacroEngine()

    def render(
        self,
        char: CharacterCard,
        *,
        user_name: str = "User",
        system_prompt: str = "",
        persona_description: str = "",
        wi_before: str = "",
        wi_after: str = "",
        anchor_before: str = "",
        anchor_after: str = "",
        mes_examples: str = "",
    ) -> str:
        """
        Render the story string template with the given data.

        Args:
            char: The character card.
            user_name: User's display name.
            system_prompt: Resolved system prompt text.
            persona_description: User persona description.
            wi_before: World info entries injected before character data.
            wi_after: World info entries injected after character data.
            anchor_before: Extension prompts before story string.
            anchor_after: Extension prompts after story string.
            mes_examples: Formatted example messages.

        Returns:
            The rendered story string.
        """
        context = {
            "char": char.name,
            "user": user_name,
            "system": system_prompt or "",
            "description": char.description,
            "personality": char.personality,
            "scenario": char.scenario,
            "persona": persona_description or "",
            "wi_before": wi_before,
            "wiAfter": wi_after,
            "wi_after": wi_after,
            "loreBefore": wi_before,
            "loreAfter": wi_after,
            "anchor_before": anchor_before,
            "anchorBefore": anchor_before,
            "anchor_after": anchor_after,
            "anchorAfter": anchor_after,
            "mesExamples": mes_examples,
            "mesExamplesRaw": mes_examples,
        }

        try:
            template = self._env.from_string(self._template_str)
            rendered = template.render(**context)
        except TemplateError:
            # Fallback: return empty string on template errors
            return ""

        # Second pass: run macro substitution for any remaining {{macro}} patterns
        rendered = self._macro_engine.substitute(rendered, replace_character_card=True)

        # Clean up leading/trailing newlines
        rendered = rendered.lstrip("\n")
        if rendered and not rendered.endswith("\n"):
            rendered += "\n"

        return rendered


class _LenientUndefined(Undefined):
    """
    A Jinja2 undefined type that renders as empty string instead of raising.
    Mimics Handlebars' behavior of rendering missing variables as empty.
    """
    def __str__(self) -> str:
        return ""

    def __iter__(self):
        return iter([])

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _LenientUndefined) or other == "" or other is None

    def __hash__(self) -> int:
        return hash("")

    def __getattr__(self, name: str):
        return _LenientUndefined(
            hint=self._undefined_hint,
            obj=self._undefined_obj,
            name=self._undefined_name,
            exc=self._undefined_exception,
        )

    def __call__(self, *args: Any, **kwargs: Any):
        return _LenientUndefined(
            hint=self._undefined_hint,
            obj=self._undefined_obj,
            name=self._undefined_name,
            exc=self._undefined_exception,
        )


# ---------------------------------------------------------------------------
# Chat Completion Prompt Builder
# ---------------------------------------------------------------------------

class ChatCompletionBuilder:
    """
    Assembles structured message arrays for Chat Completion APIs.

    Mirrors SillyTavern's populateChatCompletion() from openai.js.
    The assembly order follows the default prompt order from PromptManager.
    """

    def __init__(self, macro_engine: MacroEngine | None = None) -> None:
        self._macro_engine = macro_engine or MacroEngine()
        self._prompt_collection = PromptCollection()

    @property
    def prompt_collection(self) -> PromptCollection:
        return self._prompt_collection

    def set_prompt(self, entry: PromptEntry) -> None:
        """Add or replace a prompt entry."""
        self._prompt_collection.remove(entry.identifier)
        self._prompt_collection.add(entry)

    def load_default_prompts(self) -> None:
        """Load the default Chat Completion prompt set."""
        for config in DEFAULT_CHAT_COMPLETION_PROMPTS:
            self.set_prompt(PromptEntry(
                identifier=config.identifier,
                content=config.content,
                role=config.role,
                enabled=config.enabled,
            ))

    def assemble(
        self,
        char: CharacterCard,
        *,
        user_name: str = "User",
        chat_history: list[ChatMessage] | None = None,
        dialogue_examples: list[list[ChatMessage]] | None = None,
        system_prompt_override: str | None = None,
        jailbreak_override: str | None = None,
        prefer_character_prompt: bool = False,
        prefer_character_jailbreak: bool = False,
    ) -> list[ChatMessage]:
        """
        Assemble the full message array for a Chat Completion API call.

        Args:
            char: The character card.
            user_name: User's display name.
            chat_history: Existing chat messages.
            dialogue_examples: List of example dialogue blocks (each block is a list of messages).
            system_prompt_override: Override for the 'main' prompt content.
            jailbreak_override: Override for the 'jailbreak' prompt content.
            prefer_character_prompt: If True, use character's system_prompt for 'main'.
            prefer_character_jailbreak: If True, use character's post_history_instructions for 'jailbreak'.

        Returns:
            Ordered list of ChatMessage objects.
        """
        self._macro_engine.set_character(char)
        self._macro_engine.set_names(user_name, char.name)

        # Apply character card overrides
        if prefer_character_prompt and char.system_prompt:
            main = self._prompt_collection.get("main")
            if main:
                main.content = char.system_prompt

        if prefer_character_jailbreak and char.post_history_instructions:
            jailbreak = self._prompt_collection.get("jailbreak")
            if jailbreak:
                jailbreak.content = char.post_history_instructions

        # Apply explicit overrides
        if system_prompt_override is not None:
            main = self._prompt_collection.get("main")
            if main:
                main.content = system_prompt_override

        if jailbreak_override is not None:
            jailbreak = self._prompt_collection.get("jailbreak")
            if jailbreak:
                jailbreak.content = jailbreak_override

        # Populate content for data-driven prompts
        self._populate_character_data(char)

        # Build the message list in prompt order
        messages: list[ChatMessage] = []
        absolute_injections: list[ChatMessage] = []

        # Marker identifiers that should be processed even with empty content
        _marker_ids = {"dialogueExamples", "chatHistory"}

        for entry in self._prompt_collection.enabled_entries():
            if not entry.content and entry.identifier not in _marker_ids:
                continue

            # Resolve macros in the content
            content = self._macro_engine.substitute(entry.content)

            if entry.injection_position == InjectionPosition.ABSOLUTE:
                absolute_injections.append(ChatMessage(
                    role=entry.role,
                    content=content,
                    injected=True,
                ))
                # Store depth info for later injection
                absolute_injections[-1]._injection_depth = entry.injection_depth  # type: ignore
                absolute_injections[-1]._injection_order = entry.injection_order  # type: ignore
                continue

            if entry.identifier == "dialogueExamples":
                if dialogue_examples:
                    for block in dialogue_examples:
                        messages.append(ChatMessage(
                            role=MessageRole.SYSTEM,
                            content="[Example Chat]",
                        ))
                        messages.extend(block)
                continue

            if entry.identifier == "chatHistory":
                if chat_history:
                    messages.extend(chat_history)
                continue

            messages.append(ChatMessage(
                role=entry.role,
                content=content,
            ))

        # Insert absolute injections at specified depths
        if absolute_injections and chat_history:
            messages = _inject_at_depth(messages, absolute_injections, chat_history)

        return messages

    def _populate_character_data(self, char: CharacterCard) -> None:
        """Fill in character-card-driven prompt entries."""
        mapping = {
            "charDescription": char.description,
            "charPersonality": char.personality,
            "scenario": char.scenario,
        }
        for identifier, value in mapping.items():
            entry = self._prompt_collection.get(identifier)
            if entry and not entry.content:
                entry.content = value


# ---------------------------------------------------------------------------
# Depth injection helper
# ---------------------------------------------------------------------------

def _inject_at_depth(
    messages: list[ChatMessage],
    injections: list[ChatMessage],
    chat_history: list[ChatMessage],
) -> list[ChatMessage]:
    """
    Inject absolute-position messages into the message array at specified depths.

    Depth is measured from the end of the chat history.
    Depth 0 = after the last message, Depth 1 = one before the last, etc.
    """
    if not injections:
        return messages

    # Group injections by depth
    depth_groups: dict[int, list[ChatMessage]] = {}
    for inj in injections:
        depth = getattr(inj, "_injection_depth", 0)
        order = getattr(inj, "_injection_order", 100)
        if depth not in depth_groups:
            depth_groups[depth] = []
        depth_groups[depth].append((order, inj))  # type: ignore

    # Sort each group by injection_order (descending = higher priority first)
    for depth in depth_groups:
        depth_groups[depth].sort(key=lambda x: x[0], reverse=True)  # type: ignore

    # Find the chatHistory region in the messages list
    # We inject relative to chat history messages
    chat_start = -1
    for i, msg in enumerate(messages):
        if not msg.injected:
            chat_start = i
            break

    if chat_start == -1:
        return messages

    # Inject from deepest to shallowest
    for depth in sorted(depth_groups.keys()):
        items = depth_groups[depth]
        inject_idx = max(chat_start, len(messages) - depth)
        for _, inj in reversed(items):
            messages.insert(inject_idx, inj)

    return messages


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def build_story_string(
    char: CharacterCard,
    *,
    template: str = DEFAULT_STORY_STRING_TEMPLATE,
    user_name: str = "User",
    system_prompt: str = "",
    persona_description: str = "",
    wi_before: str = "",
    wi_after: str = "",
    **kwargs: str,
) -> str:
    """
    Quick function to render a story string from character card data.
    """
    builder = StoryStringBuilder(template=template)
    return builder.render(
        char,
        user_name=user_name,
        system_prompt=system_prompt,
        persona_description=persona_description,
        wi_before=wi_before,
        wi_after=wi_after,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Prompt Pipeline — full orchestration
# ---------------------------------------------------------------------------

class PromptPipeline:
    """
    Full prompt assembly pipeline that orchestrates:
      1. Assemble SystemBlocks from character data, world info, extensions
      2. Apply depth injection to chat history
      3. Run TokenArbitrator to enforce budget
      4. Return final ChatMessage list

    This is the top-level entry point for generating a complete prompt.
    """

    def __init__(
        self,
        max_tokens: int,
        *,
        min_recent_messages: int = 2,
        encoding_name: str = "cl100k_base",
        macro_engine: MacroEngine | None = None,
    ) -> None:
        from .token_budget import TokenArbitrator
        self._arbitrator = TokenArbitrator(
            max_tokens=max_tokens,
            min_recent_messages=min_recent_messages,
            encoding_name=encoding_name,
        )
        self._macro_engine = macro_engine or MacroEngine()

    @property
    def arbitrator(self) -> "TokenArbitrator":
        return self._arbitrator

    def build(
        self,
        char: CharacterCard,
        *,
        user_name: str = "User",
        chat_history: list[ChatMessage] | None = None,
        system_prompt: str = "",
        persona_description: str = "",
        wi_before: str = "",
        wi_after: str = "",
        depth_items: list | None = None,
        extra_blocks: list | None = None,
        include_role_play_setting: bool = True,
    ) -> list[ChatMessage]:
        """
        Build a complete, budget-enforced message list.

        Args:
            char: Character card.
            user_name: User's display name.
            chat_history: Chat messages (chronological, oldest first).
            system_prompt: System prompt text.
            persona_description: User persona description.
            wi_before: World info before character data.
            wi_after: World info after character data.
            depth_items: DepthItem list for injection into chat history.
            extra_blocks: Additional SystemBlocks to include.

        Returns:
            Final list of ChatMessage objects, budget-enforced.

        Raises:
            TokenBudgetExceeded: If content cannot fit even after trimming.
        """
        from .token_budget import Priority, SystemBlock

        self._macro_engine.set_character(char)
        self._macro_engine.set_names(user_name, char.name)

        history = list(chat_history or [])

        # Step 1: Apply depth injection to chat history
        if depth_items:
            from .depth_injection import inject_at_depth
            history = inject_at_depth(history, depth_items)

        # Step 2: Assemble SystemBlocks
        blocks = self._assemble_blocks(
            char,
            system_prompt=system_prompt,
            persona_description=persona_description,
            wi_before=wi_before,
            wi_after=wi_after,
            include_role_play_setting=include_role_play_setting,
        )

        if extra_blocks:
            blocks.extend(extra_blocks)

        # Step 3: Run token arbitration
        trimmed_blocks, trimmed_history = self._arbitrator.apply_budget(blocks, history)

        # Step 4: Convert blocks + history to final ChatMessage list
        messages: list[ChatMessage] = []
        for block in trimmed_blocks:
            content = block.total_content()
            if content:
                messages.append(ChatMessage(role=MessageRole.SYSTEM, content=content))
        messages.extend(trimmed_history)

        return messages

    def _assemble_blocks(
        self,
        char: CharacterCard,
        *,
        system_prompt: str = "",
        persona_description: str = "",
        wi_before: str = "",
        wi_after: str = "",
        include_role_play_setting: bool = True,
    ) -> list:
        """Assemble the 6 priority SystemBlocks from character data."""
        from .token_budget import Priority, SystemBlock

        blocks: list[SystemBlock] = []

        # Priority 1: System directives (never_cut)
        main_prompt = system_prompt or f"Write {char.name}'s next reply in a fictional chat."
        blocks.append(SystemBlock(
            name="system_directives",
            content=self._macro_engine.substitute(main_prompt),
            priority=Priority.SYSTEM_DIRECTIVES,
            never_cut=True,
        ))

        # Priority 2: Role-play setting (never_cut)
        if include_role_play_setting:
            setting_parts: list[str] = []
            if char.description:
                setting_parts.append(char.description)
            if char.personality:
                setting_parts.append(f"{char.name}'s personality: {char.personality}")
            if char.scenario:
                setting_parts.append(f"Scenario: {char.scenario}")
            if persona_description:
                setting_parts.append(persona_description)
            blocks.append(SystemBlock(
                name="role_play_setting",
                content="\n".join(setting_parts),
                priority=Priority.ROLE_PLAY_SETTING,
                never_cut=True,
            ))

        # Priority 3: Chat history — handled separately via history list

        # Priority 4: Group dynamics (placeholder — populated by caller via extra_blocks)

        # Priority 5: Group memory (placeholder — populated by caller via extra_blocks)

        # Priority 6: World knowledge
        wi_parts: list[str] = []
        if wi_before:
            wi_parts.append(wi_before)
        if wi_after:
            wi_parts.append(wi_after)
        blocks.append(SystemBlock(
            name="world_knowledge",
            content="\n".join(wi_parts),
            priority=Priority.WORLD_KNOWLEDGE,
        ))

        return blocks
