---
Task ID: 09
Agent: Super Z (main)
Task: Implement Chunk 09 — Project Loader and `aip-loom validate`

Work Log:
- Pulled latest repo and read all existing modules (errors.py, schemas.py, layout.py, yaml_io.py, checksum.py, frontmatter.py, ids.py, chunk_order.py, init.py, cli.py, results.py, transaction.py, PATTERN_REGISTRY.md)
- Designed ProjectState, ChunkData, ValidationResult data structures
- Added new error codes: VALIDATION_DUPLICATE_ID, VALIDATION_BROKEN_REFERENCE, VALIDATION_MISSING_FILE, VALIDATION_CHUNK_ORDER_MISMATCH
- Added new warning codes: VALIDATION_DIRTY_CHECKSUM, VALIDATION_PENDING_REVIEW
- Implemented src/aip_loom/project.py with:
  - load_project() — single authority for loading all project files
  - validate_project() — 7 validation passes, pure (no mutations)
  - ChunkData, ProjectState, ValidationResult frozen dataclasses
  - ProjectError exception for fundamental loading failures
  - _safe_load_yaml() for best-effort per-file loading
  - _discover_chunks() for scanning chunks/ directory
  - _check_missing_files() — validation pass 1
  - _check_duplicate_ids() — validation pass 3
  - _check_broken_references() — validation pass 4
  - _check_checksums() — validation pass 5
  - _check_chunk_order() — validation pass 6
  - _check_pending_reviews() — validation pass 7
- Wired aip-loom validate command in cli.py with --chunk and --json
- Updated test_cli.py: replaced placeholder validate tests with real command tests
- Created tests/test_project.py with 50 tests covering:
  - Load project fundamentals
  - Honest partial loading (malformed YAML captured, not hidden)
  - Clean project validation
  - Duplicate ID detection
  - Broken reference detection
  - Checksum mismatch reporting (warning, not auto-fix)
  - Missing file detection
  - Chunk order issues
  - Pending review items
  - Chunk scoping
  - Validation purity (no file mutations)
  - ValidationResult and ProjectState structure
- Updated PATTERN_REGISTRY.md
- All 537 tests pass (483 original + 50 new + 4 CLI test updates, net +54)
- Committed and pushed to GitHub

Stage Summary:
- src/aip_loom/project.py: 520+ lines, new module
- src/aip_loom/errors.py: +7 new error/warning codes
- src/aip_loom/cli.py: validate command wired, no longer stub
- tests/test_project.py: 50 new tests
- tests/test_cli.py: 6 validate tests replacing 2 placeholder tests
- Zero regressions across 537 total tests
