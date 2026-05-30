# AIP_Loom Pattern Registry

This registry is the single source of truth for reusable implementation
patterns in AIP_Loom.  Every chunk after Chunk 01 must read this registry
before implementation and must not create duplicate implementations of
patterns registered here.

## Error and Result Handling
- Shared envelope: src/aip_loom/results.py *(implemented — Chunk 01)*
- Error taxonomy: src/aip_loom/errors.py *(implemented — Chunk 01)*
- Renderer: src/aip_loom/output.py *(implemented — Chunk 01)*

## YAML and Schema Handling
- **YAML IO: src/aip_loom/yaml_io.py** *(implemented — Chunk 03)*
  - This is the **single, exclusive gateway** for all YAML reading and writing
    in AIP_Loom.  No other module may import or call ``ruamel.yaml`` (or any
    YAML library such as PyYAML) directly.  Direct ruamel/PyYAML imports
    elsewhere are a **spec violation** unless explicitly justified and
    registered here.
  - Provides: ``load_yaml()``, ``load_yaml_string()``, ``load_yaml_as()``,
    ``load_yaml_string_as()``, ``dump_yaml()``, ``dump_yaml_string()``.
  - Two strictness modes via ``YamlMode``: ``PROJECT`` (lenient on anchors)
    and ``UPDATE_BLOCK`` (strict: anchors/aliases/tags are hard errors).
  - Duplicate key detection via ruamel.yaml's ``DuplicateKeyError``.
  - Anchor/alias/tag scanning with ``_has_real_anchor()``, ``_scan_for_anchors_aliases()``,
    ``_scan_for_tags()`` — only real anchors (``anchor.value is not None``)
    are flagged, not the default ``Anchor(None)`` ruamel.yaml attaches.
  - Pydantic bridge: ``load_yaml_as()`` converts ruamel.yaml types to plain
    Python via ``_convert_to_plain()`` (handles CommentedMap, CommentedSeq,
    TaggedScalar, ScalarString) then validates against a Pydantic model.
  - All failures raise ``YamlLoadError`` with a ``LoomError`` carrying stable
    error codes (YAML_PARSE_ERROR, YAML_DUPLICATE_KEYS, YAML_ANCHORS_ALIASES,
    YAML_TAGS_REJECTED, SCHEMA_VALIDATION_FAILED, FILE_NOT_FOUND, etc.).
  - **Honest failure guarantee**: malformed/empty YAML never returns empty
    dict, empty list, or fabricated default state.
  - Round-trip preservation: comments, blank lines, and key order are preserved
    on human-editable files via ruamel.yaml round-trip mode.
- **Pydantic schemas: src/aip_loom/schemas.py** *(implemented — Chunk 02)*
  - This is the **single owner** of all Pydantic models. No other module may
    define private schema variants. If a command needs a new shape, it must
    be added here and tests must cover it. Duplicate private Pydantic models
    elsewhere are a spec violation.
  - Contains: enums (ReviewState, ProjectType, ChunkStatus, UpdateMode,
    ThreadState), schema version validation, ChunkFrontmatter, LedgerEntryBase
    + DecisionEntry/ThreadEntry/QuestionEntry, Ledger files (DecisionLedger,
    ThreadLedger, QuestionLedger), DistillateNode + Distillate, SessionEntry +
    SessionLog, CommentEntry + CommentLog, ProjectManifest, UpdateBlock +
    update item models.

## Filesystem and Transactions
- ProjectLayout: src/aip_loom/layout.py *(not yet implemented — Chunk 05)*
- Atomic write: src/aip_loom/fs.py *(not yet implemented — Chunk 05)*
- Locking: src/aip_loom/lock.py *(not yet implemented — Chunk 05)*
- Transaction workspace: src/aip_loom/transaction.py *(not yet implemented — Chunk 07)*

## Git
- Git wrapper: src/aip_loom/git.py *(not yet implemented — Chunk 06)*

## Validation
- Project loader: src/aip_loom/project.py *(not yet implemented — Chunk 09)*
- Validation passes: src/aip_loom/validate.py *(not yet implemented — Chunk 09)*

## IDs, Checksums, and Chunk Order
- ID allocator: src/aip_loom/ids.py *(not yet implemented — Chunk 04)*
- Checksum: src/aip_loom/checksum.py *(not yet implemented — Chunk 04)*
- Frontmatter: src/aip_loom/frontmatter.py *(not yet implemented — Chunk 04)*
- Chunk order: src/aip_loom/chunk_order.py *(not yet implemented — Chunk 04)*

## Brief / Context Selection
- Brief context engine: src/aip_loom/brief_context.py *(not yet implemented — Chunk 11)*
- Token counting: src/aip_loom/tokens.py *(not yet implemented — Chunk 11)*

## Reconcile
- Update parser: src/aip_loom/update_parser.py *(not yet implemented — Chunk 13)*
- Reconcile planner: src/aip_loom/reconcile_plan.py *(not yet implemented — Chunk 14)*
- Reconcile apply: src/aip_loom/reconcile_apply.py *(not yet implemented — Chunk 15)*

## CLI and Output
- CLI entry point: src/aip_loom/cli.py *(implemented — Chunk 01)*
- Result rendering: src/aip_loom/output.py *(implemented — Chunk 01)*
