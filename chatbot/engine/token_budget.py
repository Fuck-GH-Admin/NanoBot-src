"""
SillyTavern Token Arbitration Engine.

Implements a priority-based token budget system that enforces a strict
trimming hierarchy. Blocks are trimmed as whole units (never truncated).
Chat history is trimmed from oldest to newest, with a configurable
minimum number of recent messages always preserved.

Trimming priority (higher number = trimmed first):
    1 = system_directives   (never_cut=True)
    2 = role_play_setting   (never_cut=True)
    3 = chat_history        (shift from oldest, keep min_recent)
    4 = group_dynamics      (pop items)
    5 = group_memory        (pop items)
    6 = world_knowledge     (pop items)

Performance note:
    Uses a two-phase counting strategy:
    Phase 1: Rough estimate via len(text) / 3.35 (cheap)
    Phase 2: Precise count via tiktoken (only when estimate is close to budget)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

import tiktoken

from .prompt_builder import ChatMessage, MessageRole


# ---------------------------------------------------------------------------
# Priority levels
# ---------------------------------------------------------------------------

class Priority(IntEnum):
    """Trimming priority. Lower number = more important = trimmed last."""
    SYSTEM_DIRECTIVES = 1   # never_cut
    ROLE_PLAY_SETTING = 2   # never_cut
    CHAT_HISTORY = 3        # shift from oldest, keep min_recent
    GROUP_DYNAMICS = 4      # pop items
    GROUP_MEMORY = 5        # pop items
    WORLD_KNOWLEDGE = 6     # pop items


# ---------------------------------------------------------------------------
# SystemBlock
# ---------------------------------------------------------------------------

@dataclass
class SystemBlock:
    """
    A block of system-level content that participates in token arbitration.

    Attributes:
        name: Human-readable identifier (e.g. "world_knowledge").
        content: The main content string (used if items is empty).
        priority: Trimming priority (lower = more important).
        never_cut: If True, this block is never trimmed (priorities 1 & 2).
        items: Optional list of sub-items that can be individually popped.
               When trimming, items are popped from the END (last item first).
    """
    name: str
    content: str = ""
    priority: int = Priority.WORLD_KNOWLEDGE
    never_cut: bool = False
    items: list[str] = field(default_factory=list)

    def total_content(self) -> str:
        """Return the effective content string for token counting."""
        parts: list[str] = []
        if self.content:
            parts.append(self.content)
        if self.items:
            parts.extend(self.items)
        return "\n".join(parts)

    def item_count(self) -> int:
        """Number of trimmable items (0 if no items list)."""
        return len(self.items)


# ---------------------------------------------------------------------------
# Token counting (two-phase: estimate then precise)
# ---------------------------------------------------------------------------

# Rough estimate: characters / 3.35 ≈ tokens (for Latin-heavy text)
_CHARS_PER_TOKEN_ESTIMATE = 3.35


def estimate_tokens(text: str) -> int:
    """Rough token estimate using character count. Very cheap."""
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN_ESTIMATE)


def estimate_message_tokens(message: ChatMessage) -> int:
    """Rough token estimate for a ChatMessage."""
    overhead = 4  # role + formatting overhead
    return overhead + estimate_tokens(message.content) + estimate_tokens(message.name)


def estimate_block_tokens(block: SystemBlock) -> int:
    """Rough token estimate for a SystemBlock."""
    return estimate_tokens(block.total_content()) + 4  # +4 for formatting


class TokenCounter:
    """
    Precise token counter using tiktoken.
    Used for final validation after rough estimates indicate we're near the budget.
    """

    _OVERHEAD = {"gpt-3.5-turbo-0301": 4}
    _DEFAULT_OVERHEAD = 3
    _PADDING = 3

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        try:
            self._encoding = tiktoken.get_encoding(encoding_name)
        except Exception:
            self._encoding = tiktoken.get_encoding("cl100k_base")
        self._model: str = ""

    def set_model(self, model: str) -> None:
        self._model = model

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        return len(self._encoding.encode(text))

    def count_message(self, message: ChatMessage) -> int:
        overhead = self._OVERHEAD.get(self._model, self._DEFAULT_OVERHEAD)
        tokens = overhead
        tokens += len(self._encoding.encode(message.role.value))
        if message.content:
            tokens += len(self._encoding.encode(message.content))
        if message.name:
            tokens += len(self._encoding.encode(message.name))
            tokens += 1
        return tokens

    def count_messages(self, messages: list[ChatMessage]) -> int:
        total = sum(self.count_message(m) for m in messages)
        total += self._PADDING
        return total

    def count_block(self, block: SystemBlock) -> int:
        """Precise token count for a SystemBlock."""
        return self.count_text(block.total_content()) + self._PADDING


# ---------------------------------------------------------------------------
# TokenBudgetExceededError
# ---------------------------------------------------------------------------

class TokenBudgetExceeded(Exception):
    """
    Raised when the context cannot fit within the token budget
    even after all trimmable content has been removed.

    Attributes:
        remaining_blocks: The best-effort blocks that were kept.
        remaining_history: The best-effort chat history that was kept.
    """

    def __init__(
        self,
        message: str,
        remaining_blocks: list[SystemBlock] | None = None,
        remaining_history: list[ChatMessage] | None = None,
    ) -> None:
        super().__init__(message)
        self.remaining_blocks = remaining_blocks or []
        self.remaining_history = remaining_history or []


# ---------------------------------------------------------------------------
# TokenArbitrator
# ---------------------------------------------------------------------------

class TokenArbitrator:
    """
    Priority-based token arbitration engine.

    Receives a list of SystemBlocks and a chat history, then trims content
    according to the priority hierarchy until total tokens fit within budget.

    Trimming strategy:
      1. Never-cut blocks (priorities 1 & 2) are preserved.
      2. world_knowledge (6): items popped from end until empty.
      3. group_memory (5): items popped from end until empty.
      4. group_dynamics (4): items popped from end until empty.
      5. chat_history (3): messages shifted from oldest, keeping at least min_recent.
      6. If still over budget after all trimming → TokenBudgetExceeded.

    Performance:
      Uses len/3.35 estimates during iterative trimming.
      Switches to precise tiktoken counting for the final validation.
    """

    def __init__(
        self,
        max_tokens: int,
        *,
        min_recent_messages: int = 2,
        encoding_name: str = "cl100k_base",
    ) -> None:
        """
        Args:
            max_tokens: Maximum allowed tokens for the entire context.
            min_recent_messages: Minimum number of most-recent chat messages to preserve.
            encoding_name: tiktoken encoding name.
        """
        self._max_tokens = max_tokens
        self._min_recent = min_recent_messages
        self._counter = TokenCounter(encoding_name)

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def counter(self) -> TokenCounter:
        return self._counter

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_budget(
        self,
        blocks: list[SystemBlock],
        chat_history: list[ChatMessage],
    ) -> tuple[list[SystemBlock], list[ChatMessage]]:
        """
        Apply the token budget, trimming blocks and history as needed.

        Args:
            blocks: List of SystemBlocks (will be sorted by priority).
            chat_history: Chat messages in chronological order (oldest first).

        Returns:
            Tuple of (trimmed_blocks, trimmed_history).

        Raises:
            TokenBudgetExceeded: If even the minimal uncuttable content exceeds budget.
        """
        # Deep copy to avoid mutating inputs
        blocks = [self._copy_block(b) for b in blocks]
        history = list(chat_history)

        # Sort blocks by priority (lower number = more important = last to trim)
        blocks.sort(key=lambda b: b.priority)

        # Separate never-cut and trimmable blocks
        never_cut = [b for b in blocks if b.never_cut]
        trimmable = [b for b in blocks if not b.never_cut]

        # Sort trimmable by priority descending (highest number = trimmed first)
        trimmable.sort(key=lambda b: b.priority, reverse=True)

        # Phase 1: Iterative trimming using rough estimates
        blocks, history = self._trim_loop(never_cut, trimmable, history)

        # Phase 2: Precise validation with tiktoken
        total = self._count_precise(blocks, history)
        if total > self._max_tokens:
            raise TokenBudgetExceeded(
                f"Context requires {total} tokens (max {self._max_tokens}) "
                f"even after all trimmable content removed.",
                remaining_blocks=blocks,
                remaining_history=history,
            )

        return blocks, history

    # ------------------------------------------------------------------
    # Internal trimming loop
    # ------------------------------------------------------------------

    def _trim_loop(
        self,
        never_cut: list[SystemBlock],
        trimmable: list[SystemBlock],
        history: list[ChatMessage],
    ) -> tuple[list[SystemBlock], list[ChatMessage]]:
        """Iteratively trim until estimate fits or nothing more can be trimmed."""
        # Initial rough estimate
        total = self._estimate_total(never_cut, trimmable, history)
        if total <= self._max_tokens:
            return never_cut + trimmable, history

        # Trim trimmable blocks (already sorted by priority descending)
        for block in trimmable:
            if self._estimate_total(never_cut, trimmable, history) <= self._max_tokens:
                break
            if block.items:
                self._pop_items(block, never_cut, trimmable, history)
            # If block has no items, it's a single-content block — skip it
            # (we don't drop whole non-item blocks unless they have items to pop)

        # Trim chat history if still over budget
        if self._estimate_total(never_cut, trimmable, history) > self._max_tokens:
            history = self._trim_history(history, never_cut, trimmable)

        return never_cut + trimmable, history

    def _pop_items(
        self,
        block: SystemBlock,
        never_cut: list[SystemBlock],
        trimmable: list[SystemBlock],
        history: list[ChatMessage],
    ) -> None:
        """Pop items from a block one at a time until estimate fits or items exhausted."""
        while block.items:
            if self._estimate_total(never_cut, trimmable, history) <= self._max_tokens:
                break
            block.items.pop()

    def _trim_history(
        self,
        history: list[ChatMessage],
        never_cut: list[SystemBlock],
        trimmable: list[SystemBlock],
    ) -> list[ChatMessage]:
        """Shift messages from the oldest end, keeping at least min_recent."""
        while len(history) > self._min_recent:
            if self._estimate_total(never_cut, trimmable, history) <= self._max_tokens:
                break
            history.pop(0)  # Remove oldest
        return history

    # ------------------------------------------------------------------
    # Token counting
    # ------------------------------------------------------------------

    def _estimate_total(
        self,
        never_cut: list[SystemBlock],
        trimmable: list[SystemBlock],
        history: list[ChatMessage],
    ) -> int:
        """Rough token estimate for the entire context."""
        total = 0
        for b in never_cut:
            total += estimate_block_tokens(b)
        for b in trimmable:
            total += estimate_block_tokens(b)
        for m in history:
            total += estimate_message_tokens(m)
        total += 10  # priming / overhead
        return total

    def _count_precise(
        self,
        blocks: list[SystemBlock],
        history: list[ChatMessage],
    ) -> int:
        """Precise token count using tiktoken."""
        total = 0
        for b in blocks:
            total += self._counter.count_block(b)
        total += self._counter.count_messages(history)
        return total

    @staticmethod
    def _copy_block(block: SystemBlock) -> SystemBlock:
        """Deep copy a SystemBlock."""
        return SystemBlock(
            name=block.name,
            content=block.content,
            priority=block.priority,
            never_cut=block.never_cut,
            items=list(block.items),
        )


# ---------------------------------------------------------------------------
# Legacy classes (kept for backward compatibility)
# ---------------------------------------------------------------------------

class ChatCompletionBudget:
    """Legacy token budget manager. Kept for backward compatibility."""

    def __init__(self, context: int, response: int) -> None:
        self._token_budget: int = context - response
        self._initial_budget: int = self._token_budget

    @property
    def remaining(self) -> int:
        return self._token_budget

    @property
    def total_budget(self) -> int:
        return self._initial_budget

    @property
    def used(self) -> int:
        return self._initial_budget - self._token_budget

    def can_afford(self, message: ChatMessage, counter: TokenCounter) -> bool:
        tokens = counter.count_message(message)
        return 0 <= self._token_budget - tokens

    def can_afford_tokens(self, tokens: int) -> bool:
        return 0 <= self._token_budget - tokens

    def allocate(self, message: ChatMessage, counter: TokenCounter, identifier: str = "") -> int:
        tokens = counter.count_message(message)
        if tokens > self._token_budget:
            raise TokenBudgetExceeded(
                f"Message requires {tokens} tokens but only {self._token_budget} remain."
            )
        self._token_budget -= tokens
        return tokens

    def free(self, tokens: int) -> None:
        self._token_budget += tokens


def trim_chat_history(
    messages: list[ChatMessage],
    counter: TokenCounter,
    budget: ChatCompletionBudget,
    *,
    preserve_first: bool = False,
) -> list[ChatMessage]:
    """Legacy chat history trimmer."""
    if not messages:
        return []
    reversed_msgs = list(reversed(messages))
    result: list[ChatMessage] = []
    for i, msg in enumerate(reversed_msgs):
        if budget.can_afford(msg, counter):
            budget.allocate(msg, counter, identifier=f"chat-{len(messages) - i}")
            result.append(msg)
        else:
            break
    result.reverse()
    if preserve_first and messages and messages[0] not in result:
        first = messages[0]
        if budget.can_afford(first, counter):
            budget.allocate(first, counter, identifier="chat-first")
            result.insert(0, first)
    return result


def trim_examples(
    examples: list[list[ChatMessage]],
    counter: TokenCounter,
    budget: ChatCompletionBudget,
) -> list[list[ChatMessage]]:
    """Legacy example trimmer."""
    result: list[list[ChatMessage]] = []
    for block in examples:
        block_tokens = counter.count_messages(block)
        if budget.can_afford_tokens(block_tokens):
            for msg in block:
                budget.allocate(msg, counter, identifier="example")
            result.append(block)
        else:
            break
    return result
