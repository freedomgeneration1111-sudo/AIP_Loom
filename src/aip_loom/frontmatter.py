"""Markdown frontmatter parser and writer for AIP_Loom.

This module is the **single authority** for parsing and writing YAML
frontmatter in chunk Markdown files.  No other module may use ad-hoc
regex or string splitting to extract frontmatter — it must call
:func:`parse_frontmatter` or :func:`write_frontmatter` here.

Design principles (BuildSpec §3A and Chunk 04 description):

- **Robust parsing**: The parser handles edge cases like missing closing
  delimiters, frontmatter at the very start of the file, and empty
  prose bodies.  It does not rely on fragile regex.
- **Structured output**: :func:`parse_frontmatter` returns a validated
  :class:`ChunkFrontmatter` instance and the raw prose body string.
  Callers never need to manually extract the ID or other fields from
  raw YAML.
- **No filename inference**: When frontmatter exists, the chunk ID is
  taken from the frontmatter, never inferred from the filename.  This
  module explicitly returns the frontmatter-parsed ID.
- **Uses yaml_io**: All YAML parsing goes through :mod:`aip_loom.yaml_io`,
  maintaining the single-gateway principle.
- **Write preserves structure**: :func:`write_frontmatter` serializes the
  frontmatter dict to YAML (via :mod:`aip_loom.yaml_io`) and
  concatenates it with the prose body, producing a valid Markdown file
  with frontmatter delimiters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import (
    FIELD_MISSING,
    PROSE_EXTRACTION_AMBIGUOUS,
    LoomError,
)
from .schemas import ChunkFrontmatter, SUPPORTED_SCHEMA_VERSION
from .yaml_io import YamlLoadError, load_yaml_string_as, dump_yaml_string

__all__ = [
    "FrontmatterParseResult",
    "FrontmatterParseError",
    "parse_frontmatter",
    "write_frontmatter",
    "split_frontmatter",
]

# ---------------------------------------------------------------------------
# Frontmatter delimiters
# ---------------------------------------------------------------------------

#: The delimiter used for YAML frontmatter in Markdown files.
FRONTMATTER_DELIMITER = "---"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FrontmatterParseError(Exception):
    """Raised when frontmatter parsing fails.

    Carries a :class:`LoomError` with a stable error code.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrontmatterParseResult:
    """The result of parsing a Markdown file with YAML frontmatter.

    Attributes
    ----------
    frontmatter:
        The validated frontmatter model instance.
    prose_body:
        The prose body text (everything after the closing ``---`` delimiter).
        Does not include the frontmatter or its delimiters.
    """

    frontmatter: ChunkFrontmatter
    prose_body: str


# ---------------------------------------------------------------------------
# Public API — split
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split a Markdown string into its YAML frontmatter and prose body.

    This function performs the raw text splitting only — it does not
    validate the YAML content.  For validated parsing, use
    :func:`parse_frontmatter`.

    The frontmatter must appear at the very start of the file, beginning
    with a ``---`` line.  A matching ``---`` line closes the frontmatter.
    Everything after the closing delimiter is the prose body.

    Parameters
    ----------
    text:
        The full Markdown file content.

    Returns
    -------
    tuple[str, str]
        A 2-tuple of ``(yaml_string, prose_body)``.  The YAML string
        does not include the ``---`` delimiters.

    Raises
    ------
    FrontmatterParseError
        If the text does not contain valid frontmatter delimiters.
    """
    if not text.startswith(FRONTMATTER_DELIMITER):
        raise FrontmatterParseError(
            LoomError(
                code=FIELD_MISSING,
                message=(
                    "Markdown file does not start with frontmatter delimiter "
                    f"({FRONTMATTER_DELIMITER!r})"
                ),
                detail={"expected_start": FRONTMATTER_DELIMITER},
            )
        )

    # Find the closing --- delimiter.
    # We search after the first line (the opening ---).
    # The closing --- must be on its own line.
    lines = text.split("\n")

    # First line must be exactly "---" (possibly with trailing whitespace)
    first_line = lines[0].rstrip()
    if first_line != FRONTMATTER_DELIMITER:
        raise FrontmatterParseError(
            LoomError(
                code=FIELD_MISSING,
                message=(
                    f"First line of file is not a frontmatter delimiter: "
                    f"{lines[0]!r}"
                ),
                detail={"first_line": lines[0]},
            )
        )

    # Search for closing --- starting from line 1
    close_index = None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == FRONTMATTER_DELIMITER:
            close_index = i
            break

    if close_index is None:
        raise FrontmatterParseError(
            LoomError(
                code=FIELD_MISSING,
                message="Frontmatter opening delimiter found but no closing delimiter",
                detail={"reason": "missing_closing_delimiter"},
            )
        )

    if close_index == 1:
        # Empty frontmatter (---\n---) — this is invalid for AIP_Loom
        raise FrontmatterParseError(
            LoomError(
                code=FIELD_MISSING,
                message="Frontmatter is empty (opening and closing delimiters are adjacent)",
                detail={"reason": "empty_frontmatter"},
            )
        )

    yaml_str = "\n".join(lines[1:close_index])
    prose_body = "\n".join(lines[close_index + 1 :])

    return yaml_str, prose_body


# ---------------------------------------------------------------------------
# Public API — parse
# ---------------------------------------------------------------------------


def parse_frontmatter(text: str) -> FrontmatterParseResult:
    """Parse a Markdown string with YAML frontmatter.

    This is the primary entry point for reading chunk files.  It splits
    the frontmatter from the prose body, validates the YAML against
    :class:`ChunkFrontmatter`, and returns a structured result.

    The chunk ID is taken from the frontmatter, never inferred from
    the filename.  If frontmatter exists but is malformed, this function
    raises rather than falling back to filename-based ID inference.

    Parameters
    ----------
    text:
        The full Markdown file content, including ``---`` delimiters.

    Returns
    -------
    FrontmatterParseResult
        A frozen dataclass with ``frontmatter`` (validated model) and
        ``prose_body`` (raw text below frontmatter).

    Raises
    ------
    FrontmatterParseError
        If the text does not contain valid frontmatter, or the YAML
        content fails schema validation.
    """
    yaml_str, prose_body = split_frontmatter(text)

    try:
        fm = load_yaml_string_as(yaml_str, ChunkFrontmatter)
    except YamlLoadError as exc:
        raise FrontmatterParseError(
            LoomError(
                code=exc.loom_error.code,
                message=f"Frontmatter YAML validation failed: {exc.loom_error.message}",
                detail=exc.loom_error.detail,
            )
        ) from exc

    return FrontmatterParseResult(frontmatter=fm, prose_body=prose_body)


# ---------------------------------------------------------------------------
# Public API — write
# ---------------------------------------------------------------------------


def write_frontmatter(
    frontmatter: ChunkFrontmatter,
    prose_body: str,
) -> str:
    """Compose a Markdown string with YAML frontmatter and prose body.

    This is the primary entry point for writing chunk files.  It
    serializes the frontmatter to YAML (via :mod:`aip_loom.yaml_io`),
    wraps it in ``---`` delimiters, and concatenates with the prose body.

    Parameters
    ----------
    frontmatter:
        A validated :class:`ChunkFrontmatter` instance.
    prose_body:
        The prose body text.

    Returns
    -------
    str
        The complete Markdown file content with frontmatter.
    """
    # Serialize the frontmatter model to a plain dict, then to YAML.
    # Use mode='json' to convert enums and other non-standard types to
    # plain JSON-serializable values that ruamel.yaml can handle.
    fm_dict = frontmatter.model_dump(mode="json")
    yaml_str = dump_yaml_string(fm_dict)

    # Strip trailing newline from YAML string (dump_yaml_string adds one)
    if yaml_str.endswith("\n"):
        yaml_str = yaml_str[:-1]

    # Compose: ---\n<yaml>\n---\n<prose>
    parts = [
        FRONTMATTER_DELIMITER,
        yaml_str,
        FRONTMATTER_DELIMITER,
        prose_body,
    ]
    return "\n".join(parts)
