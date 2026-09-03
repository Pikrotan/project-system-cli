# Project Template v1.1.0 Stable

Git-native, AI-neutral project knowledge and governance system.

This distribution contains the **project system engine**: CLI, packaged machine assets (schemas, narrative templates, policies, blueprints and GitHub templates), tests, and examples. Machine assets live in `project_cli/project_system_assets/` so ordinary wheel installs remain self-contained. A concrete product repository is instantiated with `project init`; it does **not** copy the whole distribution.

## Core model

- `docs/` — concise living narrative documentation (up to 34 canonical narrative documents).
- `knowledge/` — atomic lifecycle objects (decisions, requirements, features, questions, risks, experiments, screens, flows, entities, metrics, design changes, debts).
- `.project/policies/` — machine-readable impact, retrieval, and governance policies.
- `.generated/` — disposable indexes, graphs, reports, context packs, sync/task packs.
- Git — version history and canonical repository state.
- Humans approve meaning-changing decisions; deterministic tooling defines scope and validates integrity; AI performs semantic work inside prepared task boundaries.

## Install for development

```bash
python -m pip install --no-build-isolation -e .
project --help
```

Dependencies: Python 3.11+, PyYAML, jsonschema. `--no-build-isolation` is useful for offline/local installation when build dependencies are already installed.

## Minimal flow

```bash
project init Demo --path ./demo --type mobile_app --governance solo
cd demo
project new decision --title "Use email sign-in" --domain product --owner owner
project validate
project generate
project context project --budget small
```

## Important boundaries

The CLI does not call an LLM, approve product decisions on its own, mirror Figma, replace GitHub, or treat generated files as canonical truth. `project sync`, `project task`, and `project bootstrap` prepare deterministic work packs for an external AI/human executor.


## Stable hardening notes

- Knowledge frontmatter is parsed with a strict SafeLoader: Python object constructors are disabled, duplicate keys are rejected, YAML anchors/aliases are rejected, and object/frontmatter size limits are enforced.
- Context budgets are deterministic character ceilings derived from configured token estimates (`1 token ~= 4 chars`). They are not claims of tokenizer-exact limits. `manifest.json` records both `char_budget` and `actual_chars`.
- Blueprint `conflicts_with` is treated as a symmetric incompatibility even if only one blueprint declares the edge. Conflicts are not transitive unless explicitly declared.
- Local validation can only validate approval metadata structurally; protected branches/required reviews are the enforcement boundary for strict HITL.
