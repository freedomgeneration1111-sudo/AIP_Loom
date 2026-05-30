"""Tests for aip_loom.yaml_io — YAML IO layer with round-trip preservation.

These tests prove:
- Round-trip fidelity: load → dump → load produces identical data + preserved comments.
- Duplicate key detection and rejection.
- Anchor/alias rejection in update-block mode.
- Tag rejection.
- Pydantic bridge: load_yaml_as validates against schemas.
- Honest failure: malformed YAML never produces fake clean state.
- Empty/whitespace-only YAML is rejected, not silently treated as empty dict.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from aip_loom.errors import (
    FILE_NOT_FOUND,
    SCHEMA_VALIDATION_FAILED,
    YAML_ANCHORS_ALIASES,
    YAML_DUPLICATE_KEYS,
    YAML_PARSE_ERROR,
    YAML_TAGS_REJECTED,
)
from aip_loom.schemas import (
    SUPPORTED_SCHEMA_VERSION,
    ChunkFrontmatter,
    DecisionLedger,
    ProjectManifest,
    UpdateBlock,
)
from aip_loom.yaml_io import (
    YamlLoadError,
    YamlMode,
    dump_yaml,
    dump_yaml_string,
    load_yaml,
    load_yaml_as,
    load_yaml_string,
    load_yaml_string_as,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_V = SUPPORTED_SCHEMA_VERSION


def _write_yaml(tmp_path: Path, name: str, content: str) -> Path:
    """Write a YAML file and return its path."""
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Minimal valid YAML documents for testing
# ---------------------------------------------------------------------------

MANIFEST_YAML = textwrap.dedent(f"""\
    # Project manifest
    schema_version: "{_V}"
    name: test-novel
    project_type: novel
    created_at: "2026-05-28T12:00:00Z"
    updated_at: "2026-05-28T12:00:00Z"
""")

DECISION_LEDGER_YAML = textwrap.dedent(f"""\
    # Decisions ledger
    schema_version: "{_V}"
    entries:
      - id: D-0001
        review_state: approved
        created_at: "2026-05-28T12:00:00Z"
        summary: Use present tense
""")

FRONTMATTER_YAML = textwrap.dedent(f"""\
    schema_version: "{_V}"
    id: C-0001
    title: Chapter One
    word_count: 500
    prose_checksum: abc123
    created_at: "2026-05-28T12:00:00Z"
    updated_at: "2026-05-28T12:00:00Z"
""")

UPDATE_BLOCK_YAML = textwrap.dedent(f"""\
    schema_version: "{_V}"
    fence_type: loom-update
    mode: full_replacement
    target_chunk: C-0001
    revised_prose: "The quick brown fox."
    change_summary: Revised opening.
    requires_human_review: true
""")


# ===========================================================================
# load_yaml — basic loading
# ===========================================================================


class TestLoadYamlBasic:
    """Basic YAML loading tests."""

    def test_load_valid_manifest(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, "manifest.yaml", MANIFEST_YAML)
        data = load_yaml(p)
        assert data["schema_version"] == _V
        assert data["name"] == "test-novel"
        assert data["project_type"] == "novel"

    def test_load_preserves_comments(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, "manifest.yaml", MANIFEST_YAML)
        data = load_yaml(p)
        # ruamel.yaml preserves comment metadata on the CommentedMap
        # The comment "# Project manifest" should be accessible
        assert hasattr(data, "ca")  # CommentedMap has comment attribute

    def test_load_preserves_key_order(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, "manifest.yaml", MANIFEST_YAML)
        data = load_yaml(p)
        keys = list(data.keys())
        assert keys[0] == "schema_version"
        assert keys[1] == "name"

    def test_load_with_nested_structure(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent(f"""\
            schema_version: "{_V}"
            name: test-novel
            chunks:
              order:
                - C-0001
                - C-0002
        """)
        p = _write_yaml(tmp_path, "manifest.yaml", yaml_content)
        data = load_yaml(p)
        assert data["chunks"]["order"] == ["C-0001", "C-0002"]


# ===========================================================================
# load_yaml — error cases
# ===========================================================================


class TestLoadYamlErrors:
    """YAML loading must fail honestly — never produce fake clean state."""

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml(tmp_path / "nonexistent.yaml")
        assert exc_info.value.loom_error.code == FILE_NOT_FOUND

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, "empty.yaml", "")
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml(p)
        assert exc_info.value.loom_error.code == YAML_PARSE_ERROR

    def test_whitespace_only_file_rejected(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, "ws.yaml", "   \n  \n\t\n")
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml(p)
        assert exc_info.value.loom_error.code == YAML_PARSE_ERROR

    def test_comments_only_file_rejected(self, tmp_path: Path) -> None:
        """A file with only comments and no data is not a valid YAML document."""
        p = _write_yaml(tmp_path, "comments.yaml", "# just a comment\n# another\n")
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml(p)
        assert exc_info.value.loom_error.code == YAML_PARSE_ERROR

    def test_malformed_yaml_rejected(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            name: test
              bad_indent: oops
                deeper: broken
        """)
        p = _write_yaml(tmp_path, "bad.yaml", yaml_content)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml(p)
        assert exc_info.value.loom_error.code == YAML_PARSE_ERROR

    def test_duplicate_keys_rejected(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent(f"""\
            schema_version: "{_V}"
            name: first
            name: second
        """)
        p = _write_yaml(tmp_path, "dup.yaml", yaml_content)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml(p)
        assert exc_info.value.loom_error.code == YAML_DUPLICATE_KEYS

    def test_duplicate_nested_keys_rejected(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent(f"""\
            schema_version: "{_V}"
            name: test
            chunks:
              order:
                - C-0001
              order:
                - C-0002
        """)
        p = _write_yaml(tmp_path, "dup_nested.yaml", yaml_content)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml(p)
        assert exc_info.value.loom_error.code == YAML_DUPLICATE_KEYS


# ===========================================================================
# Anchor/alias rejection
# ===========================================================================


class TestAnchorAliasRejection:
    """Anchors and aliases must be rejected in update-block mode."""

    def test_anchor_rejected_in_update_block_mode(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            schema_version: "0.1.0"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            revised_prose: &anchor_text "The quick brown fox."
            change_summary: *anchor_text
        """)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_string(yaml_content, mode=YamlMode.UPDATE_BLOCK)
        assert exc_info.value.loom_error.code == YAML_ANCHORS_ALIASES

    def test_alias_rejected_in_update_block_mode(self) -> None:
        yaml_content = textwrap.dedent("""\
            common: &common
              key: value
            derived:
              <<: *common
              extra: data
        """)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_string(yaml_content, mode=YamlMode.UPDATE_BLOCK)
        assert exc_info.value.loom_error.code == YAML_ANCHORS_ALIASES

    def test_anchor_allowed_in_project_mode(self) -> None:
        """In PROJECT mode, anchors/aliases are tolerated (lenient)."""
        yaml_content = textwrap.dedent("""\
            name: &name_val test-novel
            ref: *name_val
        """)
        # Should NOT raise — project mode is lenient
        data = load_yaml_string(yaml_content, mode=YamlMode.PROJECT)
        assert data["ref"] == "test-novel"


# ===========================================================================
# Tag rejection
# ===========================================================================


class TestTagRejection:
    """Explicit tags are rejected in both modes."""

    def test_standard_tag_accepted_project_mode(self) -> None:
        """Standard tags like !!str are accepted in project mode."""
        yaml_content = textwrap.dedent("""\
            name: !!str test-novel
        """)
        # !!str is a standard tag, should be accepted
        data = load_yaml_string(yaml_content, mode=YamlMode.PROJECT)
        # ruamel.yaml may wrap !!str in a TaggedScalar; _convert_to_plain
        # handles this when going through load_yaml_string_as, but the raw
        # CommentedMap may contain a TaggedScalar.  Verify it's loadable.
        from aip_loom.yaml_io import _convert_to_plain
        plain = _convert_to_plain(data)
        assert plain["name"] == "test-novel"

    def test_non_standard_tag_rejected_project_mode(self) -> None:
        yaml_content = textwrap.dedent("""\
            name: !custom test-novel
        """)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_string(yaml_content, mode=YamlMode.PROJECT)
        assert exc_info.value.loom_error.code == YAML_TAGS_REJECTED

    def test_python_object_tag_rejected(self) -> None:
        """The !!python/object tag is a well-known security risk."""
        yaml_content = textwrap.dedent("""\
            exploit: !!python/object/apply:os.system ["echo pwned"]
        """)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_string(yaml_content, mode=YamlMode.PROJECT)
        assert exc_info.value.loom_error.code in (YAML_TAGS_REJECTED, YAML_PARSE_ERROR)


# ===========================================================================
# Pydantic bridge — load_yaml_as
# ===========================================================================


class TestLoadYamlAs:
    """Bridge from YAML to typed Pydantic models."""

    def test_load_manifest_as_model(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, "manifest.yaml", MANIFEST_YAML)
        manifest = load_yaml_as(p, ProjectManifest)
        assert manifest.name == "test-novel"
        assert manifest.schema_version == _V

    def test_load_ledger_as_model(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, "decisions.yaml", DECISION_LEDGER_YAML)
        ledger = load_yaml_as(p, DecisionLedger)
        assert len(ledger.entries) == 1
        assert ledger.entries[0].id == "D-0001"

    def test_load_frontmatter_as_model(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, "frontmatter.yaml", FRONTMATTER_YAML)
        fm = load_yaml_as(p, ChunkFrontmatter)
        assert fm.id == "C-0001"
        assert fm.title == "Chapter One"
        assert fm.word_count == 500

    def test_load_update_block_as_model(self) -> None:
        ub = load_yaml_string_as(
            UPDATE_BLOCK_YAML,
            UpdateBlock,
            mode=YamlMode.UPDATE_BLOCK,
        )
        assert ub.target_chunk == "C-0001"
        assert ub.mode.value == "full_replacement"

    def test_schema_validation_failure(self, tmp_path: Path) -> None:
        """Invalid data that doesn't match the Pydantic model is rejected."""
        yaml_content = textwrap.dedent("""\
            schema_version: "0.1.0"
            # missing required 'name' field
        """)
        p = _write_yaml(tmp_path, "bad_manifest.yaml", yaml_content)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_as(p, ProjectManifest)
        assert exc_info.value.loom_error.code == SCHEMA_VALIDATION_FAILED

    def test_bad_schema_version_rejected(self, tmp_path: Path) -> None:
        """Unsupported major schema version is a hard error."""
        yaml_content = textwrap.dedent("""\
            schema_version: "99.0.0"
            name: test-novel
        """)
        p = _write_yaml(tmp_path, "bad_version.yaml", yaml_content)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_as(p, ProjectManifest)
        assert exc_info.value.loom_error.code == SCHEMA_VALIDATION_FAILED

    def test_extra_fields_rejected(self, tmp_path: Path) -> None:
        """extra='forbid' on schemas means unknown YAML keys are errors."""
        yaml_content = textwrap.dedent(f"""\
            schema_version: "{_V}"
            name: test-novel
            unknown_field: surprise
        """)
        p = _write_yaml(tmp_path, "extra.yaml", yaml_content)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_as(p, ProjectManifest)
        assert exc_info.value.loom_error.code == SCHEMA_VALIDATION_FAILED
        # Verify the detail includes info about the extra field
        detail = exc_info.value.loom_error.detail
        assert "errors" in detail

    def test_load_yaml_string_as_with_frontmatter(self) -> None:
        fm = load_yaml_string_as(FRONTMATTER_YAML, ChunkFrontmatter)
        assert fm.id == "C-0001"


# ===========================================================================
# Round-trip fidelity
# ===========================================================================


class TestRoundTrip:
    """Load → dump → load must produce identical data and preserve comments."""

    def test_round_trip_manifest(self, tmp_path: Path) -> None:
        """Round-trip a manifest file: load → dump → load → compare."""
        p = _write_yaml(tmp_path, "manifest.yaml", MANIFEST_YAML)
        data1 = load_yaml(p)

        # Dump to a new file
        out_path = tmp_path / "manifest_out.yaml"
        dump_yaml(data1, out_path)

        # Load the dumped file
        data2 = load_yaml(out_path)

        # Compare data values
        assert dict(data1) == dict(data2)

    def test_round_trip_preserves_comments(self, tmp_path: Path) -> None:
        """Comments should survive a round trip."""
        yaml_content = textwrap.dedent(f"""\
            # Top-level comment
            schema_version: "{_V}"
            # Name of the project
            name: test-novel
            project_type: novel
        """)
        p = _write_yaml(tmp_path, "commented.yaml", yaml_content)
        data1 = load_yaml(p)

        out_path = tmp_path / "commented_out.yaml"
        dump_yaml(data1, out_path)

        data2 = load_yaml(out_path)

        # The data values must be identical
        assert data1["schema_version"] == data2["schema_version"]
        assert data1["name"] == data2["name"]
        # And comments should be preserved in the output string
        out_text = out_path.read_text(encoding="utf-8")
        assert "Top-level comment" in out_text
        assert "Name of the project" in out_text

    def test_round_trip_string_roundtrip(self) -> None:
        """dump_yaml_string → load_yaml_string round-trip."""
        original = textwrap.dedent(f"""\
            schema_version: "{_V}"
            name: test-novel
            project_type: novel
        """)
        data1 = load_yaml_string(original)
        dumped = dump_yaml_string(data1)
        data2 = load_yaml_string(dumped)

        assert dict(data1) == dict(data2)

    def test_round_trip_nested_lists(self, tmp_path: Path) -> None:
        """Lists (like chunk order) survive round-trip."""
        yaml_content = textwrap.dedent(f"""\
            schema_version: "{_V}"
            name: test-novel
            chunks:
              order:
                - C-0001
                - C-0002
                - C-0003
        """)
        p = _write_yaml(tmp_path, "nested.yaml", yaml_content)
        data1 = load_yaml(p)

        out_path = tmp_path / "nested_out.yaml"
        dump_yaml(data1, out_path)

        data2 = load_yaml(out_path)
        assert data2["chunks"]["order"] == ["C-0001", "C-0002", "C-0003"]

    def test_round_trip_ledger_with_entries(self, tmp_path: Path) -> None:
        """Ledger entries with nested dicts/lists survive round-trip."""
        data1 = load_yaml_string(DECISION_LEDGER_YAML)
        dumped = dump_yaml_string(data1)
        data2 = load_yaml_string(dumped)

        assert data1["entries"][0]["id"] == data2["entries"][0]["id"]
        assert data1["entries"][0]["summary"] == data2["entries"][0]["summary"]


# ===========================================================================
# dump_yaml
# ===========================================================================


class TestDumpYaml:
    """YAML dump functionality."""

    def test_dump_creates_parent_dirs(self, tmp_path: Path) -> None:
        """dump_yaml should create parent directories if they don't exist."""
        data = load_yaml_string(MANIFEST_YAML)
        deep_path = tmp_path / "deep" / "nested" / "dir" / "out.yaml"
        dump_yaml(data, deep_path)
        assert deep_path.exists()

    def test_dump_string_produces_valid_yaml(self) -> None:
        data = load_yaml_string(MANIFEST_YAML)
        result = dump_yaml_string(data)
        # Re-parse to verify it's valid
        reparsed = load_yaml_string(result)
        assert reparsed["name"] == "test-novel"

    def test_dump_plain_dict(self) -> None:
        """dump_yaml_string should handle plain dicts (not CommentedMap)."""
        data = {"schema_version": _V, "name": "plain", "project_type": "novel"}
        result = dump_yaml_string(data)
        reparsed = load_yaml_string(result)
        assert reparsed["name"] == "plain"


# ===========================================================================
# YamlMode and strictness
# ===========================================================================


class TestYamlModeStrictness:
    """PROJECT mode vs UPDATE_BLOCK mode strictness."""

    def test_project_mode_default(self, tmp_path: Path) -> None:
        """Default mode is PROJECT."""
        p = _write_yaml(tmp_path, "manifest.yaml", MANIFEST_YAML)
        data = load_yaml(p)  # default should be PROJECT
        assert data["name"] == "test-novel"

    def test_update_block_mode_rejects_anchors(self) -> None:
        yaml_content = textwrap.dedent("""\
            schema_version: "0.1.0"
            fence_type: loom-update
            mode: full_replacement
            target_chunk: C-0001
            revised_prose: &ref "text"
            change_summary: *ref
        """)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_string(yaml_content, mode=YamlMode.UPDATE_BLOCK)
        assert exc_info.value.loom_error.code == YAML_ANCHORS_ALIASES

    def test_project_mode_allows_anchors(self) -> None:
        """PROJECT mode is lenient on anchors/aliases."""
        yaml_content = textwrap.dedent("""\
            base: &base
              key: value
            derived:
              <<: *base
              extra: more
        """)
        data = load_yaml_string(yaml_content, mode=YamlMode.PROJECT)
        assert data["derived"]["key"] == "value"
        assert data["derived"]["extra"] == "more"


# ===========================================================================
# YamlLoadError structure
# ===========================================================================


class TestYamlLoadError:
    """YamlLoadError must carry a proper LoomError."""

    def test_error_carries_loom_error(self) -> None:
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_string("")
        err = exc_info.value.loom_error
        assert err.code == YAML_PARSE_ERROR
        assert err.message  # non-empty
        assert isinstance(err.detail, dict)

    def test_error_has_detail_with_source(self) -> None:
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_string("", source_label="test.yaml")
        err = exc_info.value.loom_error
        assert err.detail.get("source") == "test.yaml"

    def test_file_not_found_error_detail(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.yaml"
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml(missing)
        err = exc_info.value.loom_error
        assert err.code == FILE_NOT_FOUND
        assert str(missing) in err.detail.get("path", "")


# ===========================================================================
# Honest failure — no fake clean state
# ===========================================================================


class TestHonestFailure:
    """Malformed YAML must never produce empty dict, empty list, or
    fabricated default state.  Every failure must raise YamlLoadError."""

    def test_empty_string_does_not_return_empty_dict(self) -> None:
        with pytest.raises(YamlLoadError):
            load_yaml_string("")

    def test_invalid_yaml_does_not_return_empty_dict(self, tmp_path: Path) -> None:
        """Genuinely malformed YAML (unclosed flow sequence) must be rejected."""
        p = _write_yaml(tmp_path, "invalid.yaml", "[")
        with pytest.raises(YamlLoadError):
            load_yaml(p)

    def test_null_document_does_not_return_none(self) -> None:
        """YAML '---\\n...' (null document) must be rejected."""
        with pytest.raises(YamlLoadError):
            load_yaml_string("---\n...")

    def test_only_null_value_rejected(self) -> None:
        """A document that is just 'null' must be rejected."""
        with pytest.raises(YamlLoadError):
            load_yaml_string("null")

    def test_tilde_rejected(self) -> None:
        """A document that is just '~' (YAML null) must be rejected."""
        with pytest.raises(YamlLoadError):
            load_yaml_string("~")

    def test_validation_error_on_bad_data(self) -> None:
        """Schema validation errors produce YamlLoadError, not Pydantic errors."""
        bad_yaml = textwrap.dedent("""\
            schema_version: "0.1.0"
            name: ""
        """)
        # Empty name violates min_length=1 but schema_version is valid
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_string_as(bad_yaml, ProjectManifest)
        assert exc_info.value.loom_error.code == SCHEMA_VALIDATION_FAILED

    def test_unsupported_major_version_rejected(self) -> None:
        """Major version mismatch is a hard error, not a warning."""
        bad_yaml = textwrap.dedent("""\
            schema_version: "99.0.0"
            name: test
        """)
        with pytest.raises(YamlLoadError) as exc_info:
            load_yaml_string_as(bad_yaml, ProjectManifest)
        assert exc_info.value.loom_error.code == SCHEMA_VALIDATION_FAILED


# ===========================================================================
# Golden-file round-trip tests
# ===========================================================================


class TestGoldenFileRoundTrip:
    """Golden-file style round-trip tests: specific YAML strings must
    survive load → dump → load with no data loss."""

    @pytest.mark.parametrize(
        "label,content",
        [
            ("manifest", MANIFEST_YAML),
            ("ledger", DECISION_LEDGER_YAML),
            ("frontmatter", FRONTMATTER_YAML),
        ],
    )
    def test_golden_round_trip(self, label: str, content: str) -> None:
        """Each golden YAML file must round-trip without data loss."""
        data1 = load_yaml_string(content)
        dumped = dump_yaml_string(data1)
        data2 = load_yaml_string(dumped)

        # Convert to plain dicts for comparison
        from aip_loom.yaml_io import _convert_to_plain

        plain1 = _convert_to_plain(data1)
        plain2 = _convert_to_plain(data2)
        assert plain1 == plain2

    def test_golden_manifest_validates(self) -> None:
        """The golden manifest YAML must validate as a ProjectManifest."""
        manifest = load_yaml_string_as(MANIFEST_YAML, ProjectManifest)
        assert manifest.name == "test-novel"

    def test_golden_ledger_validates(self) -> None:
        """The golden decision ledger YAML must validate."""
        ledger = load_yaml_string_as(DECISION_LEDGER_YAML, DecisionLedger)
        assert len(ledger.entries) == 1

    def test_golden_frontmatter_validates(self) -> None:
        """The golden frontmatter YAML must validate."""
        fm = load_yaml_string_as(FRONTMATTER_YAML, ChunkFrontmatter)
        assert fm.id == "C-0001"

    def test_golden_update_block_validates(self) -> None:
        """The golden update block YAML must validate."""
        ub = load_yaml_string_as(
            UPDATE_BLOCK_YAML, UpdateBlock, mode=YamlMode.UPDATE_BLOCK
        )
        assert ub.target_chunk == "C-0001"
