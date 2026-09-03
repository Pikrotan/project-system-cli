# v1.1.0 Stable Verification

## Release decision

Project Template v1.1 RC2 was promoted to **v1.1.0 Stable** after the third adversarial review found no new S0/S1 blocking issues and independently reported the existing 26-test suite as passing.

## Local verification performed before promotion

- Regression/adversarial suite: **26 passed, 0 failed**.
- RC2 filesystem/path hardening retained.
- Transactional module enable/rollback retained.
- Strict frontmatter parsing retained.
- Deterministic context character ceiling retained.
- Symmetric blueprint conflicts retained.
- Packaged resources/wheel-install design retained.

## Release semantics

- **Project Template:** v1.1.0 Stable
- **CLI:** 0.1.2
- **Schema:** 1

The CLI is versioned independently from the template. No CLI code changed during the RC2 → Stable promotion, so its version was intentionally not bumped.

## Known boundaries (not release blockers)

- Local validation verifies approval metadata structurally; strict HITL enforcement belongs to protected branches and required human reviews.
- Context token budgets are deterministic character-based estimates rather than tokenizer-exact guarantees.
- Figma/Google integrations remain protocol/reference layers in v1.1 rather than live bidirectional automation.
- Very large repositories may eventually benefit from graph/index caching; this is a scaling optimization, not a correctness blocker for v1.1.0.
