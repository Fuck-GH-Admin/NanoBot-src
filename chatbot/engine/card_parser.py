"""
SillyTavern Character Card Parser.

Supports parsing character cards from:
- PNG files (tEXt chunks with 'ccv3' or 'chara' keywords, base64 encoded)
- JSON files (V1, V2, V3 formats)
- YAML files (alternative format)
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any, Union

import yaml

from .card_schema import (
    CardSpec,
    CharacterCard,
    V1CardRaw,
    V2CardRaw,
    V2CharData,
)


# ---------------------------------------------------------------------------
# PNG tEXt chunk extraction
# ---------------------------------------------------------------------------

# PNG signature
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# PNG chunk type constants
_CHUNK_IEND = b"IEND"
_CHUNK_tEXt = b"tEXt"

# Character card keywords in tEXt chunks
_KEYWORD_V3 = "ccv3"
_KEYWORD_V2 = "chara"


def _read_png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """
    Read all PNG chunks from raw file bytes.

    Returns a list of (chunk_type, chunk_data) tuples.
    Raises ValueError if the file is not a valid PNG.
    """
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("Not a valid PNG file: missing PNG signature")

    chunks: list[tuple[bytes, bytes]] = []
    offset = len(_PNG_SIGNATURE)

    while offset < len(data):
        if offset + 8 > len(data):
            break
        # Each chunk: 4 bytes length + 4 bytes type + data + 4 bytes CRC
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        # CRC is at offset + 8 + length, 4 bytes (we skip validation for speed)
        chunks.append((chunk_type, chunk_data))
        offset += 12 + length

        if chunk_type == _CHUNK_IEND:
            break

    return chunks


def _decode_tEXt_chunk(chunk_data: bytes) -> tuple[str, str]:
    """
    Decode a PNG tEXt chunk into (keyword, text).
    Format: keyword + null separator + text (Latin-1 encoded).
    """
    null_idx = chunk_data.index(b"\x00")
    keyword = chunk_data[:null_idx].decode("latin-1")
    text = chunk_data[null_idx + 1 :].decode("latin-1")
    return keyword, text


def read_character_from_png_bytes(data: bytes) -> str:
    """
    Extract character card JSON string from PNG file bytes.

    Searches for tEXt chunks with keywords 'ccv3' (V3) or 'chara' (V2).
    V3 takes precedence over V2.

    Returns the decoded JSON string.
    Raises ValueError if no character data is found.
    """
    chunks = _read_png_chunks(data)

    v3_data: str | None = None
    v2_data: str | None = None

    for chunk_type, chunk_data in chunks:
        if chunk_type != _CHUNK_tEXt:
            continue
        try:
            keyword, text = _decode_tEXt_chunk(chunk_data)
        except (ValueError, UnicodeDecodeError):
            continue

        if keyword == _KEYWORD_V3:
            try:
                v3_data = base64.b64decode(text).decode("utf-8")
            except Exception:
                pass
        elif keyword == _KEYWORD_V2:
            try:
                v2_data = base64.b64decode(text).decode("utf-8")
            except Exception:
                pass

    result = v3_data or v2_data
    if result is None:
        raise ValueError("No character card metadata found in PNG (no 'ccv3' or 'chara' tEXt chunks)")
    return result


# ---------------------------------------------------------------------------
# JSON / YAML parsing
# ---------------------------------------------------------------------------

def _parse_json_string(json_str: str) -> dict[str, Any]:
    """Parse a JSON string into a dict, raising ValueError on failure."""
    try:
        result = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from e
    if not isinstance(result, dict):
        raise ValueError(f"Expected a JSON object, got {type(result).__name__}")
    return result


def _parse_yaml_string(yaml_str: str) -> dict[str, Any]:
    """Parse a YAML string into a dict."""
    try:
        result = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}") from e
    if not isinstance(result, dict):
        raise ValueError(f"Expected a YAML mapping, got {type(result).__name__}")
    return result


def _normalize_yaml_fields(data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize YAML-specific field names to standard character card fields.
    Some YAML exports use different key names.
    """
    field_map = {
        "context": "description",
        "greeting": "first_mes",
        "example_dialogue": "mes_example",
        "world_scenario": "scenario",
    }
    for src, dst in field_map.items():
        if src in data and dst not in data:
            data[dst] = data.pop(src)
    return data


def _normalize_gradio_fields(data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize Gradio/Pygmalion format fields to standard V1 fields.
    """
    field_map = {
        "char_name": "name",
        "char_persona": "description",
        "char_greeting": "first_mes",
        "example_dialogue": "mes_example",
        "world_scenario": "scenario",
    }
    for src, dst in field_map.items():
        if src in data and dst not in data:
            data[dst] = data.pop(src)
    return data


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def _detect_format_and_normalize(data: dict[str, Any], format: str = "auto") -> dict[str, Any]:
    """
    Detect the card format (V1, V2/V3, Gradio, YAML) and normalize to a standard form.
    Returns a dict ready to be fed into CharacterCard.
    """
    # Gradio/Pygmalion format detection
    if "char_name" in data:
        data = _normalize_gradio_fields(data)

    # YAML-specific field normalization (context -> description, greeting -> first_mes, etc.)
    # Apply when explicitly parsing YAML or when YAML-style fields are detected
    if format == "yaml" or any(k in data for k in ("context", "greeting")):
        data = _normalize_yaml_fields(data)

    return data


def parse_character_card(data: Union[str, bytes, dict[str, Any]], *, format: str = "auto") -> CharacterCard:
    """
    Parse a character card from various input formats.

    Args:
        data: The input data. Can be:
            - str: JSON or YAML string, or file path
            - bytes: PNG file bytes
            - dict: Already-parsed dictionary
        format: Force a specific format. One of:
            - "auto": Auto-detect (default)
            - "json": Force JSON parsing
            - "yaml": Force YAML parsing
            - "png": Force PNG parsing (data must be bytes)

    Returns:
        A normalized CharacterCard instance.
    """
    raw_dict: dict[str, Any]

    if isinstance(data, dict):
        raw_dict = data
    elif isinstance(data, bytes):
        json_str = read_character_from_png_bytes(data)
        raw_dict = _parse_json_string(json_str)
    elif isinstance(data, str):
        # Check if it looks like a file path
        path = Path(data)
        if path.exists() and path.is_file():
            file_bytes = path.read_bytes()
            suffix = path.suffix.lower()
            if suffix == ".png" or format == "png":
                json_str = read_character_from_png_bytes(file_bytes)
                raw_dict = _parse_json_string(json_str)
            elif suffix in (".yaml", ".yml") or format == "yaml":
                raw_dict = _parse_yaml_string(file_bytes.decode("utf-8"))
            else:
                raw_dict = _parse_json_string(file_bytes.decode("utf-8"))
        else:
            # Treat as raw string content
            if format == "yaml":
                raw_dict = _parse_yaml_string(data)
            elif format == "json":
                raw_dict = _parse_json_string(data)
            else:
                # Auto-detect: try JSON first, then YAML
                try:
                    raw_dict = _parse_json_string(data)
                except ValueError:
                    raw_dict = _parse_yaml_string(data)
    else:
        raise TypeError(f"Unsupported data type: {type(data).__name__}")

    # Normalize fields
    raw_dict = _detect_format_and_normalize(raw_dict, format=format)

    # Determine V1 vs V2/V3 and construct CharacterCard
    if "spec" in raw_dict:
        # V2/V3 format
        card = V2CardRaw.model_validate(raw_dict).to_character_card()
    else:
        # V1 format
        card = V1CardRaw.model_validate(raw_dict).to_character_card()

    return card


def write_character_to_png_bytes(
    png_data: bytes,
    card_json: str,
    *,
    write_v3: bool = True,
) -> bytes:
    """
    Write character card data into PNG tEXt chunks.

    Removes existing 'chara' and 'ccv3' chunks, then writes new ones.
    Always writes V2 ('chara'). Optionally also writes V3 ('ccv3').

    Args:
        png_data: Original PNG file bytes.
        card_json: The character card JSON string to embed.
        write_v3: Whether to also write a V3 chunk (default True).

    Returns:
        New PNG file bytes with embedded character data.
    """
    if not png_data.startswith(_PNG_SIGNATURE):
        raise ValueError("Not a valid PNG file")

    chunks = _read_png_chunks(png_data)

    # Filter out existing character card chunks
    filtered: list[tuple[bytes, bytes]] = []
    for chunk_type, chunk_data in chunks:
        if chunk_type == _CHUNK_tEXt:
            try:
                keyword, _ = _decode_tEXt_chunk(chunk_data)
                if keyword in (_KEYWORD_V2, _KEYWORD_V3):
                    continue
            except (ValueError, UnicodeDecodeError):
                pass
        filtered.append((chunk_type, chunk_data))

    # Prepare new tEXt chunks
    v2_b64 = base64.b64encode(card_json.encode("utf-8")).decode("latin-1")
    v2_chunk_data = f"{_KEYWORD_V2}\x00{v2_b64}".encode("latin-1")

    # Find IEND position for insertion
    iend_idx = len(filtered)
    for i, (ct, _) in enumerate(filtered):
        if ct == _CHUNK_IEND:
            iend_idx = i
            break

    # Insert before IEND
    new_text_chunks = [(_CHUNK_tEXt, v2_chunk_data)]

    if write_v3:
        try:
            card_dict = json.loads(card_json)
            card_dict["spec"] = "chara_card_v3"
            card_dict["spec_version"] = "3.0"
            v3_json = json.dumps(card_dict, ensure_ascii=False)
            v3_b64 = base64.b64encode(v3_json.encode("utf-8")).decode("latin-1")
            v3_chunk_data = f"{_KEYWORD_V3}\x00{v3_b64}".encode("latin-1")
            new_text_chunks.append((_CHUNK_tEXt, v3_chunk_data))
        except (json.JSONDecodeError, KeyError):
            pass  # Silently skip V3 if JSON is malformed

    filtered[iend_idx:iend_idx] = new_text_chunks

    # Rebuild PNG
    output = bytearray(_PNG_SIGNATURE)
    for chunk_type, chunk_data in filtered:
        output.extend(struct.pack(">I", len(chunk_data)))
        output.extend(chunk_type)
        output.extend(chunk_data)
        # Compute CRC over type + data
        crc = _crc32(chunk_type + chunk_data)
        output.extend(struct.pack(">I", crc))

    return bytes(output)


# ---------------------------------------------------------------------------
# CRC32 for PNG chunks
# ---------------------------------------------------------------------------

def _crc32(data: bytes) -> int:
    """Compute CRC32 matching PNG specification (polynomial 0xEDB88320)."""
    import binascii
    return binascii.crc32(data) & 0xFFFFFFFF
