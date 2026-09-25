# Project Rules

The repository is a Git-native project knowledge system. `main` is the canonical repository state; each atomic object's lifecycle status determines whether it is current truth.

Human approval is required according to `.project/policies/governance.yaml`. Local validation proves structural evidence, not human identity; strict enforcement belongs in protected hosting workflows.

Atomic objects carry lifecycle facts. Narrative docs explain current intent and system behavior. Project Skills under `.agents/skills/**` orchestrate repeatable work but are never facts, policies, schemas, approvals, or permission grants. Generated files are disposable and must be regenerated rather than edited as truth.

For semantic writes, the effective scope is the intersection of one Skill's `max_writes`, the prepared task/SYNC allowed write set, and governance authorization. Multiple Skills contribute a union of their independently valid intersections; partial authority from different Skills cannot be composed. Deterministic command outputs under `.generated/**` are command-owned and are not Skill `max_writes`.

Figma is visual truth where referenced. Google Docs are projections. Structured design input is a proposal until a human-approved SYNC lifecycle establishes canonical state. Sync means making affected canonical material consistent after an approved change, not appending text everywhere.
