"""
SillyTavern API Formatters.

Converts internal ChatMessage lists into provider-specific API formats.
Supports OpenAI, Anthropic Claude, and text completion APIs.

Based on SillyTavern's prompt-converters.js and chat-completions.js.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from .prompt_builder import ChatMessage, MessageRole


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseFormatter(ABC):
    """Abstract base for all API formatters."""

    @abstractmethod
    def format_messages(self, messages: list[ChatMessage]) -> Any:
        """Convert ChatMessage list into provider-specific format."""
        ...


# ---------------------------------------------------------------------------
# OpenAI / Chat Completion formatter
# ---------------------------------------------------------------------------

class OpenAIFormatter(BaseFormatter):
    """
    Formats ChatMessage lists into OpenAI Chat Completion API format.

    Output: list of {"role": "...", "content": "...", "name"?} dicts.
    """

    _NAME_SANITIZE = re.compile(r"[^a-zA-Z0-9_-]")
    _NAME_MAX_LEN = 64

    def __init__(self) -> None:
        self._names_behavior: int = 0  # 0=NONE, 1=DEFAULT, 2=CONTENT, 3=COMPLETION

    def set_names_behavior(self, behavior: int) -> None:
        self._names_behavior = behavior

    @classmethod
    def sanitize_name(cls, name: str) -> str:
        """Remove illegal characters and truncate to 64 chars."""
        cleaned = cls._NAME_SANITIZE.sub("", name)
        return cleaned[: cls._NAME_MAX_LEN]

    def format_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.role.value
            content = msg.content
            name = ""

            if msg.name and self._names_behavior >= 2:
                if self._names_behavior == 2:
                    content = f"{msg.name}: {msg.content}"
                elif self._names_behavior == 3:
                    name = self.sanitize_name(msg.name)

            entry: dict[str, Any] = {"role": role, "content": content}
            if name:
                entry["name"] = name
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            result.append(entry)

        return result

    def format_request(
        self,
        messages: list[ChatMessage],
        *,
        model: str = "gpt-4",
        max_tokens: int = 2048,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": self.format_messages(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        body.update(kwargs)
        return body


# ---------------------------------------------------------------------------
# Anthropic / Claude formatter
# ---------------------------------------------------------------------------

class AnthropicFormatter(BaseFormatter):
    """
    Formats ChatMessage lists into Anthropic Claude API format.

    Key transformations:
    1. ALL system messages are extracted into a single top-level 'system' string.
    2. Remaining messages have system→user role conversion.
    3. Consecutive same-role messages are merged (\\n\\n separator).
    4. First message is forced to be 'user' (empty placeholder if needed).
    5. Supports prefill (assistant message at end).
    """

    def __init__(self) -> None:
        self._prefill: str = ""

    def set_prefill(self, text: str) -> None:
        self._prefill = text

    def format_messages(self, messages: list[ChatMessage]) -> dict[str, Any]:
        # Step 1: Extract ALL system messages
        system_parts: list[str] = []
        non_system: list[ChatMessage] = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                text = msg.content
                if msg.name:
                    text = f"{msg.name}: {text}"
                system_parts.append(text)
            else:
                non_system.append(msg)

        # Step 2: Convert roles (system→user already handled above)
        converted: list[dict[str, Any]] = []
        for msg in non_system:
            role = msg.role.value  # user or assistant
            content = msg.content
            if msg.name:
                content = f"{msg.name}: {content}"
            converted.append({"role": role, "content": content})

        # Step 3: Force first message to be user
        if converted and converted[0]["role"] != "user":
            converted.insert(0, {"role": "user", "content": ""})

        # Step 4: Merge consecutive same-role messages
        merged = self._merge_consecutive(converted)

        # Step 5: Add prefill
        if self._prefill:
            merged.append({"role": "assistant", "content": self._prefill})

        return {
            "system": "\n\n".join(system_parts),
            "messages": merged,
        }

    def format_request(
        self,
        messages: list[ChatMessage],
        *,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 2048,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> dict[str, Any]:
        formatted = self.format_messages(messages)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **formatted,
        }
        body.update(kwargs)
        return body

    @staticmethod
    def _merge_consecutive(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not messages:
            return []
        merged: list[dict[str, Any]] = [messages[0].copy()]
        for msg in messages[1:]:
            if msg["role"] == merged[-1]["role"]:
                merged[-1]["content"] = merged[-1]["content"] + "\n\n" + msg["content"]
            else:
                merged.append(msg.copy())
        return merged


# Backward-compatible alias
ClaudeFormatter = AnthropicFormatter


# ---------------------------------------------------------------------------
# Text Completion formatter
# ---------------------------------------------------------------------------

class TextCompletionFormatter:
    """Formats ChatMessage lists into a flat text string for text completion APIs."""

    def __init__(self, user_name: str = "User", char_name: str = "Char") -> None:
        self._user_name = user_name
        self._char_name = char_name

    def format(self, messages: list[ChatMessage]) -> str:
        parts: list[str] = []
        for msg in messages:
            role = msg.role
            name = msg.name
            content = msg.content

            if role == MessageRole.SYSTEM:
                label = name or "System"
                parts.append(f"{label}: {content}")
            elif role == MessageRole.USER:
                label = name or self._user_name
                parts.append(f"{label}: {content}")
            elif role == MessageRole.ASSISTANT:
                label = name or self._char_name
                parts.append(f"{label}: {content}")

        result = "\n".join(parts)
        if result:
            result += "\nassistant:"
        return result


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def to_openai_format(
    messages: list[ChatMessage],
    *,
    model: str = "gpt-4",
    max_tokens: int = 2048,
    temperature: float = 0.7,
    **kwargs: Any,
) -> dict[str, Any]:
    formatter = OpenAIFormatter()
    return formatter.format_request(
        messages, model=model, max_tokens=max_tokens, temperature=temperature, **kwargs,
    )


def to_claude_format(
    messages: list[ChatMessage],
    *,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 2048,
    temperature: float = 0.7,
    prefill: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    formatter = AnthropicFormatter()
    if prefill:
        formatter.set_prefill(prefill)
    return formatter.format_request(
        messages, model=model, max_tokens=max_tokens, temperature=temperature, **kwargs,
    )


def to_text_completion(
    messages: list[ChatMessage],
    *,
    user_name: str = "User",
    char_name: str = "Char",
) -> str:
    formatter = TextCompletionFormatter(user_name=user_name, char_name=char_name)
    return formatter.format(messages)
