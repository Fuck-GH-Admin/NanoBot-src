"""
SillyTavern Lorebook (World Info) Engine.

Implements keyword-based scanning, recursive cascade activation,
and deterministic position-based classification of world-info entries.

Scanning algorithm:
  1. For each entry, check primary keywords against scan text.
  2. If primary match, evaluate secondary keywords via selective logic gate.
  3. Negative keywords (prefixed with '-') veto activation immediately.
  4. Newly activated entries' content feeds back into the scan text (cascade).
  5. Loop until no new activations or max_depth reached.

Position classification:
  Activated entries are sorted deterministically by:
    order ASC → depth ASC → content hash ASC
  and grouped into wiBefore, wiAfter, wiDepth buckets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class WorldInfoLogic(IntEnum):
    """Selective logic gates for secondary keyword matching."""
    AND_ANY = 0    # ANY primary + ANY secondary
    NOT_ALL = 1    # ANY primary + at least one secondary NOT matched
    NOT_ANY = 2    # ANY primary + NO secondary matched
    AND_ALL = 3    # ANY primary + ALL secondary matched


class WorldInfoPosition(IntEnum):
    """Where activated world-info content is injected."""
    BEFORE = 0
    AFTER = 1
    AN_TOP = 2
    AN_BOTTOM = 3
    AT_DEPTH = 4
    EM_TOP = 5
    EM_BOTTOM = 6
    OUTLET = 7


# ---------------------------------------------------------------------------
# LorebookEntry (Pydantic V2)
# ---------------------------------------------------------------------------

class LorebookEntry(BaseModel):
    """A single world-info / lorebook entry."""

    model_config = ConfigDict(extra="allow")

    uid: int = 0
    key: list[str] = []
    keysecondary: list[str] = []
    content: str = ""
    comment: str = ""
    position: int = WorldInfoPosition.BEFORE
    depth: int = 4
    order: int = 100
    selectiveLogic: int = WorldInfoLogic.AND_ANY
    disable: bool = False
    constant: bool = False
    selective: bool = False
    excludeRecursion: bool = False
    preventRecursion: bool = False
    group: str = ""
    groupOverride: bool = False
    groupWeight: int = 100
    probability: int = 100
    useProbability: bool = True
    outletName: str = ""
    role: int = 0

    @field_validator("position", mode="before")
    @classmethod
    def coerce_position(cls, v: Any) -> int:
        if isinstance(v, str):
            return int(v)
        return v

    @field_validator("selectiveLogic", mode="before")
    @classmethod
    def coerce_selective_logic(cls, v: Any) -> int:
        if isinstance(v, str):
            return int(v)
        return v


# ---------------------------------------------------------------------------
# ScanResult
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """
    Output of a lorebook scan.

    Attributes:
        wi_before: Concatenated content for position BEFORE.
        wi_after: Concatenated content for position AFTER.
        wi_depth: List of dicts for AT_DEPTH injection
                  (each has 'depth', 'order', 'content', 'role').
        activated_uids: Set of activated entry UIDs.
    """
    wi_before: str = ""
    wi_after: str = ""
    wi_depth: list[dict[str, Any]] = field(default_factory=list)
    activated_uids: set[int] = field(default_factory=set)

    def total_content(self) -> str:
        """All content joined (for token counting)."""
        parts: list[str] = []
        if self.wi_before:
            parts.append(self.wi_before)
        if self.wi_after:
            parts.append(self.wi_after)
        for d in self.wi_depth:
            if d.get("content"):
                parts.append(d["content"])
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Default constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_DEPTH = 10
DEFAULT_SCAN_DEPTH = 2


# ---------------------------------------------------------------------------
# LorebookEngine
# ---------------------------------------------------------------------------

class LorebookEngine:
    """
    World-info scanning engine.

    Scans text against lorebook entries, activates matching entries
    (including cascading / recursive activation), and classifies
    results by injection position.
    """

    def __init__(
        self,
        max_depth: int = DEFAULT_MAX_DEPTH,
        scan_depth: int = DEFAULT_SCAN_DEPTH,
        case_sensitive: bool = False,
        recursive: bool = True,
    ) -> None:
        self._max_depth = max_depth
        self._scan_depth = scan_depth
        self._case_sensitive = case_sensitive
        self._recursive = recursive

    @property
    def max_depth(self) -> int:
        return self._max_depth

    # ------------------------------------------------------------------
    # Keyword matching
    # ------------------------------------------------------------------

    @staticmethod
    def check_keywords(entry: LorebookEntry, scan_text: str) -> bool:
        """
        Check whether an entry's keywords match the scan text.

        Algorithm:
          1. Extract positive keys and negative keys (prefixed with '-').
          2. Any negative key hit → immediate False (veto).
          3. No positive keys → False.
          4. At least one positive key must match (ANY).
          5. If selective=True and secondary keys exist, evaluate via selectiveLogic.

        Matching: case-insensitive substring.
        """
        if not scan_text:
            return False

        keys = entry.key
        if not keys:
            return False

        text_lower = scan_text.lower()

        # Separate positive and negative keys
        positive_keys: list[str] = []
        negative_keys: list[str] = []
        for k in keys:
            if k.startswith("-") and len(k) > 1:
                negative_keys.append(k[1:])
            else:
                positive_keys.append(k)

        # Step 1: Negative veto
        for neg in negative_keys:
            if neg.lower() in text_lower:
                return False

        # Step 2: Must have positive keys
        if not positive_keys:
            return False

        # Step 3: At least one positive key must match
        primary_match = False
        for pk in positive_keys:
            if pk.lower() in text_lower:
                primary_match = True
                break

        if not primary_match:
            return False

        # Step 4: Secondary keyword logic
        if not entry.selective:
            return True

        secondary = entry.keysecondary
        if not secondary:
            return True

        logic = entry.selectiveLogic
        sec_lower = [s.lower() for s in secondary]

        if logic == WorldInfoLogic.AND_ANY:
            # ANY secondary matches
            for s in sec_lower:
                if s in text_lower:
                    return True
            return False

        elif logic == WorldInfoLogic.NOT_ALL:
            # At least one secondary does NOT match
            for s in sec_lower:
                if s not in text_lower:
                    return True
            return False

        elif logic == WorldInfoLogic.NOT_ANY:
            # NO secondary matches
            for s in sec_lower:
                if s in text_lower:
                    return False
            return True

        elif logic == WorldInfoLogic.AND_ALL:
            # ALL secondary must match
            for s in sec_lower:
                if s not in text_lower:
                    return False
            return True

        return True

    # ------------------------------------------------------------------
    # Recursive scan
    # ------------------------------------------------------------------

    def recursive_scan(
        self,
        entries: list[LorebookEntry],
        initial_text: str,
        context: dict[str, Any] | None = None,
    ) -> ScanResult:
        """
        Scan text against entries with recursive cascade activation.

        Args:
            entries: List of LorebookEntry to scan.
            initial_text: The text to scan for keyword matches.
            context: Optional dict with extra scan data (e.g. semantic_hits).

        Returns:
            ScanResult with classified, sorted output.
        """
        if not entries:
            return ScanResult()

        ctx = context or {}
        extra_text = ctx.get("semantic_hits", "")

        # Working copy of scan text (grows as entries activate)
        scan_buffer = initial_text
        if extra_text:
            scan_buffer += "\n" + extra_text

        activated_uids: set[int] = set()
        activated_entries: list[LorebookEntry] = []

        for _depth in range(self._max_depth):
            new_activations: list[LorebookEntry] = []

            for entry in entries:
                # Skip already activated
                if entry.uid in activated_uids:
                    continue
                # Skip disabled
                if entry.disable:
                    continue

                # Skip entries that can't be activated during recursion (depth > 0)
                if _depth > 0 and entry.excludeRecursion:
                    continue

                # Check activation
                activated = False

                if entry.constant:
                    activated = True
                elif self.check_keywords(entry, scan_buffer):
                    activated = True

                if activated:
                    activated_uids.add(entry.uid)
                    new_activations.append(entry)
                    activated_entries.append(entry)

            # No new activations → done
            if not new_activations:
                break

            # Cascade: add content of newly activated entries to scan buffer
            if self._recursive:
                for e in new_activations:
                    if not e.preventRecursion and e.content:
                        scan_buffer += "\n" + e.content

        # Classify and sort
        return self.classify_by_position(activated_entries, activated_uids)

    # ------------------------------------------------------------------
    # Classification & deterministic sorting
    # ------------------------------------------------------------------

    def classify_by_position(
        self,
        entries: list[LorebookEntry],
        activated_uids: set[int] | None = None,
    ) -> ScanResult:
        """
        Classify activated entries by position with deterministic sorting.

        Sort within each position bucket:
          order ASC → depth ASC → content hash ASC

        Args:
            entries: Activated entries (already filtered).
            activated_uids: Set of activated UIDs (for the result).

        Returns:
            ScanResult with wi_before, wi_after, wi_depth populated.
        """
        if not entries:
            return ScanResult(activated_uids=activated_uids or set())

        # Deterministic sort: order ASC → depth ASC → content string ASC
        # Using content string (not hash) for cross-process determinism.
        sorted_entries = sorted(entries, key=lambda e: (e.order, e.depth, e.content))

        wi_before_parts: list[str] = []
        wi_after_parts: list[str] = []
        wi_depth_groups: dict[tuple[int, int], list[str]] = {}  # (depth, role) → [content]

        for e in sorted_entries:
            content = e.content.strip()
            if not content:
                continue

            pos = e.position
            if pos == WorldInfoPosition.BEFORE:
                wi_before_parts.append(content)
            elif pos == WorldInfoPosition.AFTER:
                wi_after_parts.append(content)
            elif pos == WorldInfoPosition.AT_DEPTH:
                key = (e.depth, e.role)
                wi_depth_groups.setdefault(key, []).append(content)
            # ANTop, ANBottom, EMTop, EMBottom, OUTLET — not yet implemented
            # (same pattern, can be added when needed)

        # Build wi_depth list (sorted by depth ASC)
        wi_depth: list[dict[str, Any]] = []
        for (depth, role), contents in sorted(wi_depth_groups.items()):
            wi_depth.append({
                "depth": depth,
                "order": 100,  # default — individual entries already sorted
                "content": "\n".join(contents),
                "role": role,
            })

        return ScanResult(
            wi_before="\n".join(wi_before_parts),
            wi_after="\n".join(wi_after_parts),
            wi_depth=wi_depth,
            activated_uids=activated_uids or set(),
        )
