# Project Template v1.1.0 — Stable Architecture

## Layers

1. Canonical Atomic Knowledge Objects
2. Canonical Narrative Docs
3. Generated Views / Context Packs
4. Blueprints / Optional Modules
5. Machine Schemas and Policies
6. Git / PR / Human Approval Governance
7. External-system references and projections
8. Drift / validation / code-derived reference boundary

## Fundamental rule

**Deterministic tooling decides scope and validity. Semantic editors propose changes inside that scope. Humans approve meaning-changing decisions.**

The distribution and concrete project are separate: CLI and packaged machine assets (schemas/blueprints/templates/policies) live in the distribution; instantiated repositories contain only active project knowledge, policies, generated artifacts, GitHub configuration, and the project's own code structure.


## Distribution packaging boundary

Machine assets are canonical inside the installable `project_system_assets` Python package. A normal wheel install therefore does not depend on the original source checkout path. Concrete projects still receive only materialized active project files.
