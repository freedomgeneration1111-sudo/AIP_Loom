"""Single, exclusive gateway for all YAML reading and writing in AIP_Loom.

This module is the **only** place that imports or calls ``ruamel.yaml`` (or any
YAML library).  All other modules must go through this gateway.

Design principles (BuildSpec §3 and §3A):

- **Round-trip mode**: Uses ``ruamel.yaml`` in round-trip (``rt``) mode so
  that comments, blank lines, and key order are preserved on
  human-editable files.
- **Duplicate key detection**: YAML files with duplicate mapping keys are
  rejected with ``YAML_DUPLICATE_KEYS``.  This is critical for
  correctness — duplicate keys silently discard data in most YAML parsers.
- **Anchor/alias/tag rejection**: Update-block YAML is strictly validated:
  anchors, aliases, and explicit tags are rejected.  Project YAML is more
  lenient but still flags tags.
- **Pydantic bridge**: :func:`load_yaml_as` loads YAML, validates it against
  a Pydantic model from :mod:`aip_loom.schemas`, and returns a typed model
  instance.  Validation errors are converted to proper :class:`LoomError`
  instances.
- **Honest failure**: Malformed or unreadable YAML **never** produces an
  empty dict, empty list, or fabricated default state.  Every parse failure
  raises :class:`YamlLoadError` with a stable error code from
  :mod:`aip_loom.errors`.

Two loading modes govern strictness:

- ``YamlMode.PROJECT`` — for human-edited project files (manifest, ledgers,
  distillate).  Comments and key order are preserved.  Anchors/aliases are
  allowed but warned.
- ``YamlMode.UPDATE_BLOCK`` — for model-output update blocks.  Much
  stricter: anchors, aliases, and tags are hard errors.
"""

from __future__ import annotations

import io
import re
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.comments import TaggedScalar
from ruamel.yaml.scalarstring import ScalarString

from .errors import (
    FILE_NOT_FOUND,
    FILE_READ_ERROR,
    SCHEMA_VALIDATION_FAILED,
    YAML_ANCHORS_ALIASES,
    YAML_DUPLICATE_KEYS,
    YAML_PARSE_ERROR,
    YAML_ROUND_TRIP_MISMATCH,
    YAML_TAGS_REJECTED,
    LoomError,
)
from .schemas import SchemaVersionCheck

# Re-export so downstream code never needs to import ruamel.yaml types
__all__ = [
    "YamlMode",
    "YamlLoadError",
    "load_yaml",
    "load_yaml_as",
    "load_yaml_string",
    "load_yaml_string_as",
    "dump_yaml",
    "dump_yaml_string",
]

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# YamlMode — controls strictness
# ---------------------------------------------------------------------------


class YamlMode(str, Enum):
    """Strictness mode for YAML loading.

    ``PROJECT``
        For human-edited project files (manifest, ledgers, distillate).
        Comments and key order are preserved.  Anchors/aliases produce a
        warning but are not hard errors (they may appear in hand-written
        files).

    ``UPDATE_BLOCK``
        For model-output update blocks.  Much stricter: anchors, aliases,
        and explicit tags are hard errors because they are a security
        boundary — model output must not use YAML features that can
        obfuscate data or create reference cycles.
    """

    PROJECT = "project"
    UPDATE_BLOCK = "update_block"


# ---------------------------------------------------------------------------
# YamlLoadError — raised on any YAML loading failure
# ---------------------------------------------------------------------------


class YamlLoadError(Exception):
    """Raised when YAML loading fails for any reason.

    Carries a :class:`LoomError` with a stable error code so that the
    caller can construct a proper :class:`CommandResult`.
    """

    def __init__(self, loom_error: LoomError) -> None:
        self.loom_error = loom_error
        super().__init__(loom_error.message)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_yaml() -> YAML:
    """Create a ``ruamel.yaml.YAML`` instance configured for round-trip mode.

    The instance is configured to:
    - Preserve comments and ordering (round-trip mode).
    - Allow unicode.
    - Not emit ``---`` at the start of documents (unless the original had it).
    """
    y = YAML()
    y.preserve_quotes = True
    y.allow_unicode = True
    y.default_flow_style = False
    # Explicit start (---) only if the original had it
    y.explicit_start = None
    return y


def _has_real_anchor(obj: Any) -> bool:
    """Check whether a ruamel.yaml object has a real (non-None) anchor.

    ruamel.yaml always attaches an ``Anchor`` attribute to ``CommentedMap``
    and ``CommentedSeq``, even when no anchor was in the YAML source.
    A real anchor has ``anchor.value is not None``.
    """
    anchor = getattr(obj, "anchor", None)
    if anchor is None:
        return False
    # Anchor.value is None when no actual anchor existed in the source
    return getattr(anchor, "value", None) is not None


def _scan_for_anchors_aliases(data: Any, path: str = "") -> list[str]:
    """Walk a ruamel.yaml data structure and collect all anchor/alias references.

    Returns a list of human-readable paths like ``"root.keys[2]"`` for
    diagnostic purposes.  Only reports real anchors (those that existed
    in the YAML source), not the default ``Anchor(None)`` that ruamel.yaml
    attaches to every container.
    """
    findings: list[str] = []

    if isinstance(data, CommentedMap):
        if _has_real_anchor(data):
            findings.append(f"{path or 'root'} (anchor: {data.anchor.value})")
        for key in data:
            # Check for anchors on keys (rare but possible)
            if _has_real_anchor(key):
                findings.append(
                    f"{path or 'root'}.key({key!r}) (anchor: {key.anchor.value})"
                )
            child_path = f"{path}.{key}" if path else str(key)
            findings.extend(_scan_for_anchors_aliases(data[key], child_path))
    elif isinstance(data, CommentedSeq):
        if _has_real_anchor(data):
            findings.append(f"{path or 'root'} (anchor: {data.anchor.value})")
        for i, item in enumerate(data):
            child_path = f"{path}[{i}]" if path else f"[{i}]"
            findings.extend(_scan_for_anchors_aliases(item, child_path))
    else:
        # Scalars (including ScalarString subclasses) may carry anchors
        if _has_real_anchor(data):
            findings.append(f"{path or 'root'} (anchor: {data.anchor.value})")

    return findings


def _scan_for_tags(data: Any, path: str = "") -> list[str]:
    """Walk a ruamel.yaml data structure and collect explicit non-standard tags.

    Tags like ``!!python/object`` or custom application tags are a security
    risk and must be rejected.  Standard YAML tags (``!!str``, ``!!int``,
    etc.) are permitted.
    """
    findings: list[str] = []

    # Standard tags that are always safe
    _STANDARD_TAGS = {
        "tag:yaml.org,2002:map",
        "tag:yaml.org,2002:seq",
        "tag:yaml.org,2002:str",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:null",
        "tag:yaml.org,2002:timestamp",
        "tag:yaml.org,2002:merge",
    }

    if isinstance(data, CommentedMap):
        tag = getattr(data, "tag", None)
        if tag and tag.value and tag.value not in _STANDARD_TAGS:
            findings.append(f"{path or 'root'} (tag: {tag.value})")
        for key in data:
            child_path = f"{path}.{key}" if path else str(key)
            findings.extend(_scan_for_tags(data[key], child_path))
    elif isinstance(data, CommentedSeq):
        tag = getattr(data, "tag", None)
        if tag and tag.value and tag.value not in _STANDARD_TAGS:
            findings.append(f"{path or 'root'} (tag: {tag.value})")
        for i, item in enumerate(data):
            child_path = f"{path}[{i}]" if path else f"[{i}]"
            findings.extend(_scan_for_tags(item, child_path))
    elif isinstance(data, TaggedScalar):
        # TaggedScalar is produced by explicit tags like !!str, !!int etc.
        tag = getattr(data, "tag", None)
        if tag and tag.value and tag.value not in _STANDARD_TAGS:
            findings.append(f"{path or 'root'} (tag: {tag.value})")
        # Recurse into the value if it's a container
        val = data.value if hasattr(data, "value") else data
        if isinstance(val, (CommentedMap, CommentedSeq)):
            findings.extend(_scan_for_tags(val, path))
    else:
        # Plain scalars — check for non-standard tags
        tag = getattr(data, "tag", None)
        if tag and tag.value and tag.value not in _STANDARD_TAGS:
            findings.append(f"{path or 'root'} (tag: {tag.value})")

    return findings


def _validate_yaml_structure(
    data: Any,
    mode: YamlMode,
    source_label: str,
) -> None:
    """Post-load validation for anchors, aliases, and tags.

    In ``UPDATE_BLOCK`` mode, anchors/aliases/tags are hard errors.
    In ``PROJECT`` mode, tags are hard errors but anchors/aliases are
    tolerated (they may appear in hand-written files, though they
    shouldn't).
    """
    # Scan for tags — always rejected
    tag_findings = _scan_for_tags(data)
    if tag_findings:
        detail = ", ".join(tag_findings[:5])  # limit detail length
        raise YamlLoadError(
            LoomError(
                code=YAML_TAGS_REJECTED,
                message=f"YAML contains explicit tags in {source_label}",
                detail={"findings": detail},
            )
        )

    # Scan for anchors/aliases
    anchor_findings = _scan_for_anchors_aliases(data)
    if anchor_findings and mode == YamlMode.UPDATE_BLOCK:
        detail = ", ".join(anchor_findings[:5])
        raise YamlLoadError(
            LoomError(
                code=YAML_ANCHORS_ALIASES,
                message=(
                    f"YAML anchors/aliases are forbidden in update-block mode "
                    f"in {source_label}"
                ),
                detail={"findings": detail},
            )
        )


def _convert_to_plain(data: Any) -> Any:
    """Convert ruamel.yaml types to plain Python types.

    This is needed before passing data to Pydantic, which does not
    understand ruamel.yaml's special types (``CommentedMap``,
    ``CommentedSeq``, ``TaggedScalar``, ``ScalarString``, etc.).

    Preserves dict/list nesting but strips all ruamel.yaml metadata
    (comments, anchors, tags, etc.).
    """
    if isinstance(data, TaggedScalar):
        # Unwrap TaggedScalar to its underlying Python value
        return _convert_to_plain(data.value)
    elif isinstance(data, CommentedMap):
        return {str(k): _convert_to_plain(v) for k, v in data.items()}
    elif isinstance(data, CommentedSeq):
        return [_convert_to_plain(item) for item in data]
    elif isinstance(data, ScalarString):
        # ScalarString subclasses (PlainScalarString, etc.)
        return str(data)
    elif isinstance(data, list):
        return [_convert_to_plain(item) for item in data]
    elif isinstance(data, dict):
        return {str(k): _convert_to_plain(v) for k, v in data.items()}
    return data


# ---------------------------------------------------------------------------
# Public API — load
# ---------------------------------------------------------------------------


def load_yaml(
    path: Path,
    mode: YamlMode = YamlMode.PROJECT,
) -> Any:
    """Load a YAML file and return the parsed data structure.

    Parameters
    ----------
    path:
        Path to the YAML file.
    mode:
        Strictness mode — see :class:`YamlMode`.

    Returns
    -------
    Any
        The parsed YAML data (typically a ``CommentedMap``).

    Raises
    ------
    YamlLoadError
        On any loading failure — file not found, parse error, duplicate
        keys, forbidden anchors/aliases/tags.  The ``loom_error`` attribute
        carries a :class:`LoomError` with a stable error code.
    """
    if not path.exists():
        raise YamlLoadError(
            LoomError(
                code=FILE_NOT_FOUND,
                message=f"YAML file not found: {path}",
                detail={"path": str(path)},
            )
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise YamlLoadError(
            LoomError(
                code=FILE_READ_ERROR,
                message=f"Cannot read YAML file: {path}",
                detail={"path": str(path), "os_error": str(exc)},
            )
        ) from exc

    return load_yaml_string(raw, mode=mode, source_label=str(path))


def load_yaml_string(
    raw: str,
    mode: YamlMode = YamlMode.PROJECT,
    source_label: str = "<string>",
) -> Any:
    """Load YAML from a string and return the parsed data structure.

    Parameters
    ----------
    raw:
        The raw YAML string.
    mode:
        Strictness mode — see :class:`YamlMode`.
    source_label:
        Human-readable label for error messages (e.g. file path).

    Returns
    -------
    Any
        The parsed YAML data.

    Raises
    ------
    YamlLoadError
        On any loading failure.
    """
    if not raw.strip():
        raise YamlLoadError(
            LoomError(
                code=YAML_PARSE_ERROR,
                message=f"YAML content is empty or whitespace-only in {source_label}",
                detail={"source": source_label},
            )
        )

    yaml = _make_yaml()

    try:
        data = yaml.load(raw)
    except DuplicateKeyError as exc:
        raise YamlLoadError(
            LoomError(
                code=YAML_DUPLICATE_KEYS,
                message=f"Duplicate keys detected in YAML: {source_label}",
                detail={"source": source_label, "detail": str(exc)},
            )
        ) from exc
    except Exception as exc:
        raise YamlLoadError(
            LoomError(
                code=YAML_PARSE_ERROR,
                message=f"YAML parse error in {source_label}: {exc}",
                detail={"source": source_label, "detail": str(exc)},
            )
        ) from exc

    # ruamel.yaml may return None for a document that contains only comments
    if data is None:
        raise YamlLoadError(
            LoomError(
                code=YAML_PARSE_ERROR,
                message=(
                    f"YAML document resolved to empty/None in {source_label}: "
                    "no data found (comments-only documents are not valid)"
                ),
                detail={"source": source_label},
            )
        )

    # Post-load structural validation
    _validate_yaml_structure(data, mode, source_label)

    return data


def load_yaml_as(
    path: Path,
    model_type: type[T],
    mode: YamlMode = YamlMode.PROJECT,
) -> T:
    """Load a YAML file and validate it against a Pydantic model.

    This is the primary bridge between YAML files and typed schemas.
    It loads the YAML, converts ruamel.yaml types to plain Python,
    and validates against the given Pydantic model.

    Parameters
    ----------
    path:
        Path to the YAML file.
    model_type:
        The Pydantic model class to validate against.
    mode:
        Strictness mode.

    Returns
    -------
    T
        A validated instance of ``model_type``.

    Raises
    ------
    YamlLoadError
        On YAML loading failure or Pydantic validation failure.
    """
    data = load_yaml(path, mode=mode)
    return _validate_data_as(data, model_type, str(path))


def load_yaml_string_as(
    raw: str,
    model_type: type[T],
    mode: YamlMode = YamlMode.PROJECT,
    source_label: str = "<string>",
) -> T:
    """Load YAML from a string and validate it against a Pydantic model.

    Parameters
    ----------
    raw:
        The raw YAML string.
    model_type:
        The Pydantic model class to validate against.
    mode:
        Strictness mode.
    source_label:
        Human-readable label for error messages.

    Returns
    -------
    T
        A validated instance of ``model_type``.

    Raises
    ------
    YamlLoadError
        On YAML loading failure or Pydantic validation failure.
    """
    data = load_yaml_string(raw, mode=mode, source_label=source_label)
    return _validate_data_as(data, model_type, source_label)


def _validate_data_as(
    data: Any,
    model_type: type[T],
    source_label: str,
) -> T:
    """Validate parsed YAML data against a Pydantic model.

    Performs an early schema_version check first (using
    :class:`SchemaVersionCheck`) if the data contains a ``schema_version``
    key, so that incompatible versions are rejected before full validation.
    """
    plain = _convert_to_plain(data)

    # Early schema_version check if the field exists
    if isinstance(plain, dict) and "schema_version" in plain:
        try:
            SchemaVersionCheck(schema_version=plain["schema_version"])
        except ValidationError as exc:
            raise YamlLoadError(
                LoomError(
                    code=SCHEMA_VALIDATION_FAILED,
                    message=(
                        f"Schema version check failed for {source_label}: "
                        f"{plain['schema_version']}"
                    ),
                    detail={
                        "source": source_label,
                        "schema_version": plain["schema_version"],
                        "pydantic_errors": str(exc),
                    },
                )
            ) from exc

    # Full Pydantic validation
    try:
        return model_type(**plain)
    except ValidationError as exc:
        # Extract the most useful validation errors for the detail dict
        error_details = []
        for err in exc.errors():
            error_details.append({
                "loc": ".".join(str(l) for l in err["loc"]),
                "msg": err["msg"],
                "type": err["type"],
            })
        raise YamlLoadError(
            LoomError(
                code=SCHEMA_VALIDATION_FAILED,
                message=f"Schema validation failed for {source_label}",
                detail={
                    "source": source_label,
                    "model": model_type.__name__,
                    "errors": error_details[:10],  # limit to first 10
                    "error_count": len(exc.errors()),
                },
            )
        ) from exc


# ---------------------------------------------------------------------------
# Public API — dump
# ---------------------------------------------------------------------------


def dump_yaml(
    data: Any,
    path: Path,
) -> None:
    """Write data to a YAML file, preserving comments and order where possible.

    If *data* is a :class:`CommentedMap` or :class:`CommentedSeq` (i.e.
    it was loaded via :func:`load_yaml` and not converted), comments and
    ordering are preserved.  If *data* is a plain dict/list, it is
    serialized normally.

    Parameters
    ----------
    data:
        The data to write.  Should be a CommentedMap/CommentedSeq for
        round-trip fidelity, or a plain dict/list for new files.
    path:
        The output file path.

    Raises
    ------
    YamlLoadError
        On write failure.
    """
    yaml = _make_yaml()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
    except OSError as exc:
        raise YamlLoadError(
            LoomError(
                code=FILE_READ_ERROR,
                message=f"Cannot write YAML file: {path}",
                detail={"path": str(path), "os_error": str(exc)},
            )
        ) from exc


def dump_yaml_string(data: Any) -> str:
    """Serialize data to a YAML string, preserving comments and order.

    Parameters
    ----------
    data:
        The data to serialize.

    Returns
    -------
    str
        The YAML string.
    """
    yaml = _make_yaml()
    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()
