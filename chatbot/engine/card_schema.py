"""
SillyTavern Character Card Schema Definitions.

Defines Pydantic V2 models for V1, V2, and V3 character card specifications,
with validation, default values, and field mapping between versions.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CardSpec(str, Enum):
    V1 = "chara_card_v1"
    V2 = "chara_card_v2"
    V3 = "chara_card_v3"


class DepthRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


# ---------------------------------------------------------------------------
# Extensions sub-models
# ---------------------------------------------------------------------------

class DepthPrompt(BaseModel):
    """Character-specific depth injection prompt."""
    depth: int = Field(default=4, ge=0, description="Injection depth from end of chat")
    prompt: str = Field(default="", description="The depth prompt text")
    role: DepthRole = Field(default=DepthRole.SYSTEM, description="Message role for injection")


class CharacterExtensions(BaseModel):
    """ST-specific extension fields stored in data.extensions."""
    talkativeness: float = Field(default=0.5, ge=0.0, le=1.0)
    fav: bool = Field(default=False)
    world: str = Field(default="", description="Linked world info file name")
    depth_prompt: DepthPrompt = Field(default_factory=DepthPrompt)
    regex_scripts: list[Any] = Field(default_factory=list)

    # Non-standard extensions preserved as extra fields
    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Character Book (embedded World Info)
# ---------------------------------------------------------------------------

class CharacterBookEntry(BaseModel):
    """A single entry in the character book (world info)."""
    keys: list[str] = Field(default_factory=list, description="Primary trigger keywords")
    secondary_keys: list[str] = Field(default_factory=list)
    content: str = Field(default="")
    extensions: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = Field(default=True)
    insertion_order: int = Field(default=0)
    case_sensitive: Optional[bool] = Field(default=None)
    name: Optional[str] = Field(default=None)
    priority: Optional[int] = Field(default=None)
    id: Optional[int] = Field(default=None)
    comment: Optional[str] = Field(default=None)
    selective: Optional[bool] = Field(default=None)
    secondary: Optional[bool] = Field(default=None)
    constant: Optional[bool] = Field(default=None)
    vectorized: Optional[bool] = Field(default=None)
    position: Optional[str] = Field(default=None, description="Before/after char or at_depth")

    model_config = {"extra": "allow"}


class CharacterBook(BaseModel):
    """Embedded world info book within a character card."""
    name: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    scan_depth: Optional[int] = Field(default=None)
    token_budget: Optional[int] = Field(default=None)
    recursive_scanning: Optional[bool] = Field(default=None)
    extensions: dict[str, Any] = Field(default_factory=dict)
    entries: list[CharacterBookEntry] = Field(default_factory=list)

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# V2 Character Card Data (the inner "data" object)
# ---------------------------------------------------------------------------

class V2CharData(BaseModel):
    """
    The V2 spec's inner data object.
    All fields have defaults for robustness against incomplete cards.
    """
    name: str = Field(default="")
    description: str = Field(default="")
    personality: str = Field(default="")
    scenario: str = Field(default="")
    first_mes: str = Field(default="")
    mes_example: str = Field(default="")
    creator_notes: str = Field(default="")
    system_prompt: str = Field(default="")
    post_history_instructions: str = Field(default="")
    alternate_greetings: list[str] = Field(default_factory=list)
    character_book: Optional[CharacterBook] = Field(default=None)
    tags: list[str] = Field(default_factory=list)
    creator: str = Field(default="")
    character_version: str = Field(default="")
    extensions: CharacterExtensions = Field(default_factory=CharacterExtensions)

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Top-level Character Card (runtime representation)
# ---------------------------------------------------------------------------

class CharacterCard(BaseModel):
    """
    The unified runtime character card object.
    Combines V1 top-level fields with V2 nested data.
    After parsing/normalization, both top-level and data.* fields are populated.
    """
    # V1 top-level fields (always populated after normalization)
    name: str = Field(default="")
    description: str = Field(default="")
    personality: str = Field(default="")
    scenario: str = Field(default="")
    first_mes: str = Field(default="")
    mes_example: str = Field(default="")
    creatorcomment: str = Field(default="", description="Legacy V1 field")
    tags: list[str] = Field(default_factory=list)
    talkativeness: float = Field(default=0.5, ge=0.0, le=1.0)
    fav: bool = Field(default=False)
    create_date: str = Field(default="")

    # V2/V3 spec metadata
    spec: Optional[CardSpec] = Field(default=None)
    spec_version: Optional[str] = Field(default=None)

    # V2 nested data (always populated after normalization)
    data: V2CharData = Field(default_factory=V2CharData)

    # ST-added runtime fields
    chat: str = Field(default="", description="Current chat filename")
    avatar: str = Field(default="", description="Avatar PNG filename (unique ID)")
    json_data: str = Field(default="", description="Raw JSON string of original data")
    shallow: bool = Field(default=False, description="Lazy-loaded marker")

    model_config = {"extra": "allow"}

    # ------------------------------------------------------------------
    # Normalization validators
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def normalize_v2_to_top_level(cls, data: dict[str, Any]) -> dict[str, Any]:
        """
        If the input has a V2/V3 structure (with spec + data.* fields),
        hoist data.* fields to the top level for unified access.
        """
        if not isinstance(data, dict):
            return data

        inner = data.get("data")
        if not isinstance(inner, dict):
            return data

        # Field mapping: data.* -> top-level
        field_map = {
            "name": "name",
            "description": "description",
            "personality": "personality",
            "scenario": "scenario",
            "first_mes": "first_mes",
            "mes_example": "mes_example",
        }
        for src, dst in field_map.items():
            if src in inner and (dst not in data or not data[dst]):
                data[dst] = inner[src]

        # Extensions mapping
        ext = inner.get("extensions", {})
        if isinstance(ext, dict):
            if "talkativeness" in ext and "talkativeness" not in data:
                data["talkativeness"] = ext["talkativeness"]
            if "fav" in ext and "fav" not in data:
                data["fav"] = ext["fav"]
            if "tags" not in data and "tags" in inner:
                data["tags"] = inner["tags"]

        return data

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def system_prompt(self) -> str:
        """Character's custom system prompt from V2 data."""
        return self.data.system_prompt

    @property
    def post_history_instructions(self) -> str:
        """Character's post-history instructions from V2 data."""
        return self.data.post_history_instructions

    @property
    def depth_prompt(self) -> DepthPrompt:
        """Character's depth injection prompt."""
        return self.data.extensions.depth_prompt

    @property
    def alternate_greetings(self) -> list[str]:
        return self.data.alternate_greetings

    @property
    def character_book(self) -> Optional[CharacterBook]:
        return self.data.character_book

    @property
    def character_version(self) -> str:
        return self.data.character_version

    @property
    def creator_notes(self) -> str:
        return self.data.creator_notes


# ---------------------------------------------------------------------------
# V1 raw input model (for parsing legacy cards)
# ---------------------------------------------------------------------------

class V1CardRaw(BaseModel):
    """Raw V1 character card as imported from JSON (no spec field)."""
    name: str = Field(default="")
    description: str = Field(default="")
    personality: str = Field(default="")
    scenario: str = Field(default="")
    first_mes: str = Field(default="")
    mes_example: str = Field(default="")
    creatorcomment: str = Field(default="")
    tags: list[str] = Field(default_factory=list)
    talkativeness: float = Field(default=0.5)
    fav: bool = Field(default=False)
    create_date: str = Field(default="")

    model_config = {"extra": "allow"}

    def to_character_card(self) -> CharacterCard:
        """Convert a V1 raw card to the unified CharacterCard."""
        return CharacterCard(
            name=self.name,
            description=self.description,
            personality=self.personality,
            scenario=self.scenario,
            first_mes=self.first_mes,
            mes_example=self.mes_example,
            creatorcomment=self.creatorcomment,
            tags=list(self.tags),
            talkativeness=self.talkativeness,
            fav=self.fav,
            create_date=self.create_date,
            data=V2CharData(
                name=self.name,
                description=self.description,
                personality=self.personality,
                scenario=self.scenario,
                first_mes=self.first_mes,
                mes_example=self.mes_example,
                tags=list(self.tags),
                extensions=CharacterExtensions(
                    talkativeness=self.talkativeness,
                    fav=self.fav,
                ),
            ),
        )


# ---------------------------------------------------------------------------
# V2/V3 raw input model
# ---------------------------------------------------------------------------

class V2CardRaw(BaseModel):
    """Raw V2/V3 character card as imported from JSON (has spec field)."""
    spec: CardSpec = Field(default=CardSpec.V2)
    spec_version: str = Field(default="2.0")
    data: V2CharData = Field(default_factory=V2CharData)

    model_config = {"extra": "allow"}

    def to_character_card(self) -> CharacterCard:
        """Convert a V2/V3 raw card to the unified CharacterCard."""
        d = self.data
        ext = d.extensions
        return CharacterCard(
            name=d.name,
            description=d.description,
            personality=d.personality,
            scenario=d.scenario,
            first_mes=d.first_mes,
            mes_example=d.mes_example,
            tags=list(d.tags),
            talkativeness=ext.talkativeness,
            fav=ext.fav,
            spec=self.spec,
            spec_version=self.spec_version,
            data=d,
        )
