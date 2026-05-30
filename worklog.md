---
Task ID: 4
Agent: main
Task: Implement CHUNK 04 — IDs, Checksums, Frontmatter, and Chunk Order Helpers

Work Log:
- Cloned AIP_Loom repo and studied all existing modules (errors.py, results.py, schemas.py, yaml_io.py, output.py, cli.py)
- Performed Before-Code Ritual: cataloged files, patterns, anti-patterns, reusable patterns
- Created src/aip_loom/ids.py with allocate_next_id(), extract_id_number(), KNOWN_PREFIXES, DuplicateIdError, InvalidIdError
- Created src/aip_loom/checksum.py with compute_prose_checksum(), CHECKSUM_ALGORITHM
- Created src/aip_loom/frontmatter.py with parse_frontmatter(), write_frontmatter(), split_frontmatter(), FrontmatterParseResult, FrontmatterParseError
- Created src/aip_loom/chunk_order.py with resolve_chunk_order(), natural_sort_key(), ChunkOrderResult
- Updated .agent/PATTERN_REGISTRY.md with all four modules and mandatory usage rule
- Created 4 test files with 102 tests total
- Fixed two issues found during testing: model_dump(mode="json") for enum serialization, natural_sort_key trailing empty string in test assertions
- All 267 tests pass (102 new + 165 existing)
- Committed and pushed to remote

Stage Summary:
- 4 new production modules, 4 new test files, 1 updated pattern registry
- 102 new tests covering positive + negative cases for all four modules
- All key requirements met: canonical-only ID allocation, prose-body-only checksum, no filename inference, CHUNK_ORDER_FALLBACK_USED warning
- Commit: feat(chunk-04): implement IDs, checksums, frontmatter, and chunk order helpers
