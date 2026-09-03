# Gemini RC1 Review Disposition

This document records the team's verification of Gemini's second adversarial review against the actual RC1 code. Findings are not accepted solely because they appear in the review.

## Accepted

### Asymmetric `conflicts_with` checking
Confirmed. RC1 checked only conflicts declared by the module being enabled. RC2 checks both the incoming module's declarations and declarations made by already-enabled modules. Conflict relations are treated as symmetric, not transitive.

### Resource-exhaustion hardening for frontmatter (principle, not the stated mechanism)
The report's claim that a YAML alias “Billion Laughs” necessarily expands exponentially in memory during `yaml.safe_load` was not reproduced: PyYAML aliases share object references. Nevertheless, untrusted metadata can still be used for resource abuse. RC2 therefore rejects YAML anchors/aliases entirely in knowledge frontmatter, rejects duplicate keys, and enforces object/frontmatter size ceilings.

## Rejected / not reproduced

### Git option injection
Not present in RC1. The only subprocess invocation is a fixed argument vector: `git diff --name-only HEAD`. No user-provided branch, tag, path, or option is passed to Git.

### JSON Schema ReDoS via project input
Not reproduced. RC1 loads schemas from the trusted distribution, not from project-controlled files, and the bundled regex patterns are simple anchored fixed-form patterns. This is not a practical ReDoS path in the current implementation.

### Context budget formatting overflow
Not reproduced. RC1 computes the exact formatted block before adding it and includes the pack header in `used`; `context.md` therefore stays within the deterministic character ceiling. RC2 adds `char_budget` and `actual_chars` to the manifest to make this invariant explicit. The configured token count remains an estimate, not tokenizer-exact.

### Graph cycle returns exit code 0
Incorrect for RC1. Dependency cycles are emitted as `ERROR`; `project validate` exits with code 2 when any `ERROR` or `BLOCKING` issue exists. Isolated knowledge objects are valid and are not considered a broken graph by design.

## Additional RC2 hardening

Module rollback now also attempts to remove empty directories created by the failed enable transaction.
