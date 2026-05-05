"""
engine — SillyTavern Core Logic Library.

A pure Python library extracting the core character card parsing,
macro substitution, and prompt assembly logic from SillyTavern.
"""

from .card_schema import (
    CharacterBook,
    CharacterBookEntry,
    CharacterCard,
    CharacterExtensions,
    DepthPrompt,
    DepthRole,
    V1CardRaw,
    V2CardRaw,
    V2CharData,
)
from .card_parser import parse_character_card, read_character_from_png_bytes
from .macro_engine import MacroEngine, substitute_params
from .prompt_builder import (
    ChatCompletionBuilder,
    ChatMessage,
    InjectionPosition,
    MessageRole,
    PromptCollection,
    PromptEntry,
    PromptPipeline,
    StoryStringBuilder,
    build_story_string,
)
from .token_budget import (
    ChatCompletionBudget,
    Priority,
    SystemBlock,
    TokenArbitrator,
    TokenBudgetExceeded,
    TokenCounter,
    estimate_block_tokens,
    estimate_message_tokens,
    estimate_tokens,
    trim_chat_history,
    trim_examples,
)
from .depth_injection import (
    DepthItem,
    ExtensionPrompt,
    ExtensionPromptManager,
    ExtensionPromptType,
    inject_at_depth,
    inject_at_depth_legacy,
    inject_in_prompt,
    create_character_depth_prompt,
    create_world_info_depth_entry,
)
from .api_formatters import (
    AnthropicFormatter,
    BaseFormatter,
    ClaudeFormatter,
    OpenAIFormatter,
    TextCompletionFormatter,
    to_claude_format,
    to_openai_format,
    to_text_completion,
)
from .lorebook_engine import (
    LorebookEngine,
    LorebookEntry,
    ScanResult,
    WorldInfoLogic,
    WorldInfoPosition,
)

__all__ = [
    # card_schema
    "CharacterBook",
    "CharacterBookEntry",
    "CharacterCard",
    "CharacterExtensions",
    "DepthPrompt",
    "DepthRole",
    "V1CardRaw",
    "V2CardRaw",
    "V2CharData",
    # card_parser
    "parse_character_card",
    "read_character_from_png_bytes",
    # macro_engine
    "MacroEngine",
    "substitute_params",
    # prompt_builder
    "ChatCompletionBuilder",
    "ChatMessage",
    "InjectionPosition",
    "MessageRole",
    "PromptCollection",
    "PromptEntry",
    "PromptPipeline",
    "StoryStringBuilder",
    "build_story_string",
    # token_budget
    "ChatCompletionBudget",
    "Priority",
    "SystemBlock",
    "TokenArbitrator",
    "TokenBudgetExceeded",
    "TokenCounter",
    "estimate_block_tokens",
    "estimate_message_tokens",
    "estimate_tokens",
    "trim_chat_history",
    "trim_examples",
    # depth_injection
    "DepthItem",
    "ExtensionPrompt",
    "ExtensionPromptManager",
    "ExtensionPromptType",
    "inject_at_depth",
    "inject_at_depth_legacy",
    "inject_in_prompt",
    "create_character_depth_prompt",
    "create_world_info_depth_entry",
    # api_formatters
    "AnthropicFormatter",
    "BaseFormatter",
    "ClaudeFormatter",
    "OpenAIFormatter",
    "TextCompletionFormatter",
    "to_claude_format",
    "to_openai_format",
    "to_text_completion",
    # lorebook_engine
    "LorebookEngine",
    "LorebookEntry",
    "ScanResult",
    "WorldInfoLogic",
    "WorldInfoPosition",
]
