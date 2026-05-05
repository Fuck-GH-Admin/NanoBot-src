"""
SillyTavern Depth Injection System.

Implements the @Depth injection mechanism that inserts prompts at specific
positions within the chat history message array.

Depth semantics (matching SillyTavern's actual behavior):
  - depth 0: insert BEFORE the last message (i.e. at index len-1)
  - depth 1: insert BEFORE the second-to-last message (i.e. at index len-2)
  - depth N: insert BEFORE the (N+1)th message from the end (i.e. at index len-N-1)

Same-depth items are sorted by `order` DESCENDING before insertion
(higher order = higher priority = inserted first = appears earlier in output).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from .prompt_builder import ChatMessage, MessageRole


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

@dataclass
class DepthItem:
    """
    A single item to be injected at a specific depth in the chat history.

    Attributes:
        content: The text content to inject.
        depth: Injection depth. 0 = before last message, N = before (N+1)th from end.
        order: Priority within the same depth. Higher = inserted first.
        role: Message role for the injected message (default: system).
        key: Optional identifier for deduplication / management.
    """
    content: str
    depth: int = 0
    order: int = 100
    role: MessageRole = MessageRole.SYSTEM
    key: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.role, str):
            self.role = MessageRole(self.role)


# ---------------------------------------------------------------------------
# Enums (kept for backward compatibility)
# ---------------------------------------------------------------------------

class ExtensionPromptType(IntEnum):
    """Where an extension prompt is injected."""
    BEFORE_PROMPT = 0
    IN_PROMPT = 1
    IN_CHAT = 2


# ---------------------------------------------------------------------------
# Extension prompt (kept for backward compatibility)
# ---------------------------------------------------------------------------

@dataclass
class ExtensionPrompt:
    """An extension prompt registered for injection."""
    key: str
    value: str
    position: ExtensionPromptType = ExtensionPromptType.IN_CHAT
    depth: int = 4
    role: MessageRole = MessageRole.SYSTEM
    injection_order: int = 100
    scan: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.position, int):
            self.position = ExtensionPromptType(self.position)
        if isinstance(self.role, str):
            self.role = MessageRole(self.role)

    def to_depth_item(self) -> DepthItem:
        """Convert to the new DepthItem format."""
        return DepthItem(
            content=self.value,
            depth=self.depth,
            order=self.injection_order,
            role=self.role,
            key=self.key,
        )


# ---------------------------------------------------------------------------
# Extension prompt manager (kept for backward compatibility)
# ---------------------------------------------------------------------------

class ExtensionPromptManager:
    """Manages extension prompts and their injection into message arrays."""

    def __init__(self) -> None:
        self._prompts: dict[str, ExtensionPrompt] = {}

    def set(
        self,
        key: str,
        value: str,
        position: ExtensionPromptType = ExtensionPromptType.IN_CHAT,
        depth: int = 4,
        role: MessageRole = MessageRole.SYSTEM,
        injection_order: int = 100,
        scan: bool = False,
    ) -> None:
        if not value:
            self._prompts.pop(key, None)
            return
        self._prompts[key] = ExtensionPrompt(
            key=key, value=value, position=position,
            depth=depth, role=role, injection_order=injection_order, scan=scan,
        )

    def get(self, key: str) -> ExtensionPrompt | None:
        return self._prompts.get(key)

    def remove(self, key: str) -> None:
        self._prompts.pop(key, None)

    def get_all(self) -> list[ExtensionPrompt]:
        return list(self._prompts.values())

    def get_by_position(self, position: ExtensionPromptType) -> list[ExtensionPrompt]:
        return [p for p in self._prompts.values() if p.position == position and p.enabled and p.value]

    def clear(self) -> None:
        self._prompts.clear()


# ---------------------------------------------------------------------------
# Well-known keys
# ---------------------------------------------------------------------------

DEPTH_PROMPT = "DEPTH_PROMPT"
AUTHORS_NOTE = "NOTE_MODULE"
STORY_STRING = "STORY_STRING"


# ---------------------------------------------------------------------------
# Core depth injection algorithm
# ---------------------------------------------------------------------------

def inject_at_depth(
    messages: list[ChatMessage],
    items: list[DepthItem],
) -> list[ChatMessage]:
    """
    Inject DepthItems into the chat history at specified depths.

    Depth semantics:
      depth 0 → insert at index (len - 1), i.e. before the last message
      depth N → insert at index (len - N - 1), i.e. before the (N+1)th from end

    Same-depth items are sorted by `order` DESCENDING (higher order = earlier in output).

    Args:
        messages: Chat history in chronological order (oldest first).
        items: List of DepthItem to inject.

    Returns:
        New message list with injections inserted.
    """
    if not items:
        return list(messages)

    # Filter out empty content
    valid_items = [it for it in items if it.content]
    if not valid_items:
        return list(messages)

    result = list(messages)
    n = len(result)

    # Group items by depth
    depth_groups: dict[int, list[DepthItem]] = {}
    for it in valid_items:
        depth_groups.setdefault(it.depth, []).append(it)

    # Process each depth group
    # We need to process from deepest to shallowest so that insertions
    # at deeper positions don't shift the indices of shallower ones.
    for depth in sorted(depth_groups.keys(), reverse=True):
        group = depth_groups[depth]
        # Sort by order descending (higher order = higher priority = first)
        group.sort(key=lambda x: x.order, reverse=True)

        # Calculate insertion index
        # depth 0 → before last message → index = len - 1
        # depth N → before (N+1)th from end → index = len - N - 1
        # Clamp to valid range
        insert_idx = max(0, min(len(result) - 1, len(result) - depth - 1))

        # Insert items at the same position. Since list.insert puts the new
        # element BEFORE the existing element at that index, inserting [A, B, C]
        # in forward order at the same index yields [..., A, B, C, existing...]
        # which is the desired priority order (highest first).
        for item in result[insert_idx:insert_idx + 1]:
            pass  # just verifying the index is valid

        # Build ChatMessage objects and insert them
        injected_msgs = [
            ChatMessage(role=it.role, content=it.content, injected=True)
            for it in group
        ]
        for i, msg in enumerate(injected_msgs):
            result.insert(insert_idx + i, msg)

    return result


# ---------------------------------------------------------------------------
# Legacy inject_at_depth (backward compatible)
# ---------------------------------------------------------------------------

def inject_at_depth_legacy(
    messages: list[ChatMessage],
    injections: list[ExtensionPrompt],
    *,
    max_depth: int = 100,
) -> list[ChatMessage]:
    """Legacy interface using ExtensionPrompt. Delegates to inject_at_depth."""
    items = [
        p.to_depth_item()
        for p in injections
        if p.position == ExtensionPromptType.IN_CHAT and p.value and p.enabled
    ]
    return inject_at_depth(messages, items)


# ---------------------------------------------------------------------------
# Story string injection
# ---------------------------------------------------------------------------

def inject_in_prompt(
    story_string: str,
    injections: list[ExtensionPrompt],
    *,
    separator: str = "\n",
) -> str:
    """Inject BEFORE_PROMPT and IN_PROMPT extension prompts around the story string."""
    before_parts: list[str] = []
    after_parts: list[str] = []

    for p in injections:
        if not p.value or not p.enabled:
            continue
        if p.position == ExtensionPromptType.BEFORE_PROMPT:
            before_parts.append(p.value)
        elif p.position == ExtensionPromptType.IN_PROMPT:
            after_parts.append(p.value)

    parts: list[str] = []
    if before_parts:
        parts.append(separator.join(before_parts))
    parts.append(story_string)
    if after_parts:
        parts.append(separator.join(after_parts))

    return separator.join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_character_depth_prompt(
    char_name: str,
    prompt_text: str,
    depth: int = 4,
    role: MessageRole = MessageRole.SYSTEM,
) -> ExtensionPrompt:
    """Create an ExtensionPrompt for a character's depth prompt."""
    return ExtensionPrompt(
        key=f"{DEPTH_PROMPT}_{char_name}",
        value=prompt_text,
        position=ExtensionPromptType.IN_CHAT,
        depth=depth,
        role=role,
        injection_order=100,
    )


def create_world_info_depth_entry(
    content: str,
    depth: int,
    role: MessageRole = MessageRole.SYSTEM,
    injection_order: int = 0,
) -> ExtensionPrompt:
    """Create an ExtensionPrompt for a world info entry at depth."""
    return ExtensionPrompt(
        key=f"WI_DEPTH_{depth}_{role.value}_{injection_order}",
        value=content,
        position=ExtensionPromptType.IN_CHAT,
        depth=depth,
        role=role,
        injection_order=injection_order,
    )
