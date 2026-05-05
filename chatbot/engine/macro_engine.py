"""
SillyTavern Macro Engine.

Handles substitution of {{macro}} placeholders in prompt strings.
Supports character card fields, identity macros, datetime macros,
and custom user-defined macros.

Based on the legacy `substituteParams()` and `evaluateMacros()` functions
from SillyTavern's public/scripts/macros.js.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Union

from .card_schema import CharacterCard


# Regex matching {{macro_name}} or {{macro_name:arg}}
_MACRO_PATTERN = re.compile(r"\{\{([^{}]+?)\}\}")


class MacroEngine:
    """
    A macro substitution engine that replaces {{placeholder}} tokens
    in template strings with resolved values.

    Macros are resolved in this order:
    1. Built-in special macros ({{newline}}, {{trim}}, {{noop}}, etc.)
    2. User-registered custom macros
    3. Character card field macros ({{description}}, {{personality}}, etc.)
    4. Identity macros ({{char}}, {{user}}, {{group}}, etc.)
    5. Datetime macros ({{time}}, {{date}}, {{weekday}}, etc.)
    6. Context macros ({{maxContext}}, {{maxPrompt}}, etc.)
    """

    def __init__(self) -> None:
        self._custom_macros: dict[str, Union[str, Callable[[], str]]] = {}
        self._char: CharacterCard | None = None
        self._user_name: str = "User"
        self._char_name: str = "Char"
        self._group_members: list[str] = []
        self._max_context: int = 0
        self._max_prompt: int = 0
        self._max_response: int = 0
        self._last_message: str = ""
        self._last_user_message: str = ""
        self._last_char_message: str = ""
        self._model_name: str = ""
        self._mes_examples_formatted: str = ""
        self._mes_examples_raw: str = ""
        self._persona_description: str = ""

    # ------------------------------------------------------------------
    # Configuration methods
    # ------------------------------------------------------------------

    def set_character(self, char: CharacterCard | None) -> None:
        """Set the active character card for macro resolution."""
        self._char = char
        if char:
            self._char_name = char.name

    def set_names(self, user_name: str = "User", char_name: str = "Char") -> None:
        """Set user and character names."""
        self._user_name = user_name
        self._char_name = char_name

    def set_group_members(self, members: list[str]) -> None:
        """Set group member names (for group chat macros)."""
        self._group_members = list(members)

    def set_context_limits(
        self,
        max_context: int = 0,
        max_prompt: int = 0,
        max_response: int = 0,
    ) -> None:
        """Set token context limits for {{maxContext}}, {{maxPrompt}}, {{maxResponse}}."""
        self._max_context = max_context
        self._max_prompt = max_prompt
        self._max_response = max_response

    def set_model_name(self, name: str) -> None:
        """Set the model name for {{model}} macro."""
        self._model_name = name

    def set_mes_examples(self, formatted: str, raw: str) -> None:
        """Set pre-formatted example messages for {{mesExamples}} and {{mesExamplesRaw}}."""
        self._mes_examples_formatted = formatted
        self._mes_examples_raw = raw

    def set_persona_description(self, desc: str) -> None:
        """Set user persona description for {{persona}}."""
        self._persona_description = desc

    def set_last_messages(
        self,
        last_message: str = "",
        last_user_message: str = "",
        last_char_message: str = "",
    ) -> None:
        """Set last message values for {{lastMessage}}, {{lastUserMessage}}, {{lastCharMessage}}."""
        self._last_message = last_message
        self._last_user_message = last_user_message
        self._last_char_message = last_char_message

    def register_macro(self, name: str, value: Union[str, Callable[[], str]]) -> None:
        """
        Register a custom macro.

        Args:
            name: Macro name (without braces).
            value: Either a static string or a callable returning a string.
        """
        self._custom_macros[name] = value

    def unregister_macro(self, name: str) -> None:
        """Remove a custom macro."""
        self._custom_macros.pop(name, None)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve_macro(self, raw_key: str) -> str | None:
        """
        Resolve a single macro key to its string value.
        Returns None if the macro is not recognized.
        """
        # Split on first colon for macros with arguments (e.g., {{reverse:text}})
        if ":" in raw_key:
            key, arg = raw_key.split(":", 1)
        else:
            key, arg = raw_key, ""

        key = key.strip()

        # --- Built-in special macros ---
        if key == "newline":
            return "\n"
        if key == "noop":
            return ""
        if key == "trim":
            return ""  # Trim is handled post-substitution
        if key == "input":
            return ""  # No textarea in backend

        # --- Reverse macro ---
        if key == "reverse" and arg:
            return arg[::-1]

        # --- Comment macro ---
        if key.startswith("//"):
            return ""

        # --- Custom user macros ---
        if key in self._custom_macros:
            val = self._custom_macros[key]
            return val() if callable(val) else val

        # --- Character card field macros ---
        if self._char:
            char = self._char
            if key == "description":
                return char.description
            if key == "personality":
                return char.personality
            if key == "scenario":
                return char.scenario
            if key in ("charPrompt", "charInstruction"):
                return char.system_prompt
            if key in ("charJailbreak", "jailbreak"):
                return char.post_history_instructions
            if key in ("charVersion", "char_version"):
                return char.character_version
            if key == "charDepthPrompt":
                return char.depth_prompt.prompt
            if key == "creatorNotes":
                return char.creator_notes
            if key == "firstMessage":
                return char.first_mes

        # --- MesExamples ---
        if key == "mesExamples":
            return self._mes_examples_formatted
        if key == "mesExamplesRaw":
            return self._mes_examples_raw

        # --- Persona ---
        if key == "persona":
            return self._persona_description

        # --- Identity macros ---
        if key == "user":
            return self._user_name
        if key == "char":
            return self._char_name
        if key == "charIfNotGroup":
            if self._group_members:
                return self._char_name
            return self._char_name
        if key == "group":
            if self._group_members:
                return " and ".join(self._group_members)
            return self._char_name
        if key == "groupNotMuted":
            if self._group_members:
                return " and ".join(self._group_members)
            return self._char_name
        if key == "notChar":
            names = [n for n in self._group_members if n != self._char_name]
            return " and ".join(names) if names else self._user_name
        if key == "model":
            return self._model_name

        # --- Context limit macros ---
        if key in ("maxContext", "maxContextTokens"):
            return str(self._max_context)
        if key in ("maxPrompt", "maxPromptTokens"):
            return str(self._max_prompt)
        if key in ("maxResponse", "maxResponseTokens"):
            return str(self._max_response)

        # --- Last message macros ---
        if key == "lastMessage":
            return self._last_message
        if key == "lastUserMessage":
            return self._last_user_message
        if key == "lastCharMessage":
            return self._last_char_message

        # --- Datetime macros ---
        now = datetime.now()
        if key == "time":
            return now.strftime("%I:%M %p")
        if key == "date":
            return now.strftime("%m/%d/%Y")
        if key == "weekday":
            return now.strftime("%A")
        if key == "isotime":
            return now.strftime("%H:%M")
        if key == "isodate":
            return now.strftime("%Y-%m-%d")
        if key == "datetimeformat" and arg:
            try:
                return now.strftime(arg)
            except ValueError:
                return ""

        # Timezone-adjusted time: {{time_UTC+/-N}}
        tz_match = re.match(r"time_UTC([+-]\d+)", key)
        if tz_match:
            offset_hours = int(tz_match.group(1))
            tz = timezone(timedelta(hours=offset_hours))
            return datetime.now(tz).strftime("%I:%M %p")

        # Unknown macro — return None (leave as-is)
        return None

    # ------------------------------------------------------------------
    # Main substitution
    # ------------------------------------------------------------------

    def substitute(self, text: str, *, replace_character_card: bool = True) -> str:
        """
        Replace all {{macro}} placeholders in the input text.

        Args:
            text: The template string containing {{macro}} placeholders.
            replace_character_card: If False, skip character card field macros
                (to avoid recursive substitution when processing card fields themselves).

        Returns:
            The string with all recognized macros replaced.
        """
        if not text:
            return text

        def _replacer(match: re.Match) -> str:
            raw_key = match.group(1)

            # Skip character card macros if requested
            if not replace_character_card:
                char_macros = {
                    "description", "personality", "scenario",
                    "charPrompt", "charInstruction", "charJailbreak",
                    "charVersion", "char_version", "charDepthPrompt",
                    "creatorNotes", "firstMessage",
                    "mesExamples", "mesExamplesRaw",
                }
                check_key = raw_key.split(":")[0].strip() if ":" in raw_key else raw_key.strip()
                if check_key in char_macros:
                    return match.group(0)

            result = self._resolve_macro(raw_key)
            return result if result is not None else match.group(0)

        result = _MACRO_PATTERN.sub(_replacer, text)

        # Handle {{trim}} by removing surrounding newlines
        # (SillyTavern does this as a post-processing step)
        result = result.replace("{{trim}}", "")

        return result


# ---------------------------------------------------------------------------
# Standalone convenience function
# ---------------------------------------------------------------------------

def substitute_params(
    text: str,
    *,
    char: CharacterCard | None = None,
    user_name: str = "User",
    char_name: str = "Char",
    group_members: list[str] | None = None,
    replace_character_card: bool = True,
    **extra_macros: str,
) -> str:
    """
    Standalone function for quick macro substitution.

    Args:
        text: Template string.
        char: Character card instance.
        user_name: User's name.
        char_name: Character's name.
        group_members: Group member names.
        replace_character_card: Whether to resolve character card field macros.
        **extra_macros: Additional macros to register.

    Returns:
        The substituted string.
    """
    engine = MacroEngine()
    if char:
        engine.set_character(char)
        engine.set_names(user_name, char.name)
    else:
        engine.set_names(user_name, char_name)
    if group_members:
        engine.set_group_members(group_members)
    for k, v in extra_macros.items():
        engine.register_macro(k, v)
    return engine.substitute(text, replace_character_card=replace_character_card)
