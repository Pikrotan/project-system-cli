# Gemini Adversarial Review — Disposition for v1.1 RC1

This file records which reported findings were reproduced against the original `Project Template v1.1 Final` and how RC1 handled them.

## Accepted / reproduced

### Filesystem path traversal and symlink writes
Reproduced against `project_system/modules.py` using an isolated distribution/project copy. RC1 validates blueprint source/target paths, rejects traversal and symlink paths, and adds regression tests.

### Non-atomic module enable
Reproduced conceptually and covered with an injected mid-commit failure regression test. RC1 pre-validates/reads sources, stages writes, rolls back newly created module files on failure, and atomically replaces `project.yaml`.

### HITL boundary clarity
Accepted as a messaging/governance issue. Local validation can verify approval metadata structure but cannot prove a human personally supplied it. RC1 emits an explicit INFO boundary for active decisions; protected branches/required reviews remain the enforcement mechanism for strict governance.

### Parallel ID collision risk
The review's stated implementation (`max N + 1`) was incorrect: the original already used cryptographically random date+suffixed IDs. However, the original suffix was only 4 base32-like characters (~1M possibilities). RC1 expands this to 8 characters and uses exclusive object-file creation to close same-working-tree races.

## Rejected as factually incorrect, with regression coverage

### Unsafe YAML loading
The original `frontmatter.py` and YAML utility path already used `yaml.safe_load`. A malicious Python-object YAML tag is rejected by `ConstructorError`. RC1 adds a regression test so this remains explicit.

### Mid-object / mid-frontmatter context truncation
The original context builder already added complete blocks atomically and skipped a block if it did not fit; it did not slice YAML in the middle. The reported failure mechanism was not present.

A different real issue was found during verification: core docs were added before a targeted object, so a small budget could silently omit the target itself. RC1 fixes this by making the target first and mandatory, recording omitted docs/objects in the manifest, and failing explicitly when the target alone cannot fit.

## Additional RC1 validation hardening

- Object `type` must match its `knowledge/<directory>/` location.
- Unknown object/project schema versions are rejected (schema version is fixed to `1` for v1.1).
- Blueprint `conflicts_with` is now enforced during enable.

## Test status

RC1 local suite: see `MANIFEST.md` for the final run recorded at packaging time.
