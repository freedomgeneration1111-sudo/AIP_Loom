# Changelog

All notable changes to AIP_Loom are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2025-05-30

### Added — Phase 1 Complete

**Core Infrastructure (Chunks 01–04)**
- Project manifest and Pydantic v2 schema definitions with `extra="forbid"`
- Canonical project filesystem layout with path safety validation
- Stable error and warning code taxonomy
- Universal `CommandResult` envelope for all CLI commands
- YAML I/O with strict modes (canonical, update block, config)
- Safe file write operations with atomic writes and path validation
- Prose checksum computation (SHA-256)
- Chunk ID format validation and allocation (`C-NNNN`, `D-NNNN`, etc.)

**Project Init and Validation (Chunks 05–09)**
- `aip-loom init` — project initialization with transaction safety and best-effort Git
- `aip-loom validate` — multi-pass validation engine (missing files, schema, duplicate IDs, broken references, checksums, chunk order, pending reviews)
- `aip-loom status` — health dashboard (healthy / degraded / blocked)
- Project loader (`load_project`) — single authority for loading all canonical files
- Frontmatter parser and writer for chunk Markdown files

**Brief and Context (Chunks 10–12)**
- `aip-loom inspect` — read-only context preview for a chunk
- `aip-loom brief` — deterministic session brief generation with token budget
- Context selection engine with priority-based section ordering and budget truncation
- Token counting support (optional, via `tiktoken`)

**Reconcile Pipeline (Chunks 13–15)**
- Strict model-output parser (`update_parser.py`) — security boundary with fence validation, YAML strictness, schema validation, model-assigned ID rejection, prose extraction, and size/depth limits
- Reconcile planner (`reconcile_plan.py`) — builds execution plan from parsed update block with provisional-to-canonical ID mapping
- Transactional reconcile apply (`reconcile_apply.py`) — 14-step protocol with exclusive locking, snapshot-before-modify, rollback-on-failure, post-apply validation, archive evidence, and `RECOVERY.md` on Git failure

**Build and Locking (Chunks 06, 16)**
- Exclusive project locking (`lock.py`) — `O_CREAT|O_EXCL` atomic creation, PID liveness check, stale lock detection
- Transaction workspace (`transaction.py`) — snapshot, restore, commit, cleanup with hash verification and failure injection protocol
- `aip-loom build` — draft Markdown concatenation respecting canonical chunk order

**Acceptance and Chaos (Chunk 17)**
- End-to-end acceptance test exercising the full public CLI (init → validate → status → inspect → brief → reconcile preview → reconcile apply → build)
- Chaos/failure injection tests: crash during staged write, crash during canonical replacement, git commit failure, dirty git tree, hostile model output, stale lock, malformed YAML, missing fence, duplicate IDs, concurrent reconcile, build on invalid project

### CLI Commands

| Command | Description |
|---------|-------------|
| `aip-loom init NAME` | Initialize a new project |
| `aip-loom status` | Show project health dashboard |
| `aip-loom validate` | Validate project integrity |
| `aip-loom inspect CHUNK` | Show context for a chunk |
| `aip-loom brief CHUNK` | Generate a session brief |
| `aip-loom reconcile CHUNK -o FILE` | Reconcile model output |
| `aip-loom build` | Build draft output |

All commands support `--json` for machine-readable output.

### Dependencies

- typer>=0.12.0
- rich>=13.0.0
- pydantic>=2.0.0
- ruamel.yaml>=0.18.0
- tiktoken>=0.7.0 (optional)
