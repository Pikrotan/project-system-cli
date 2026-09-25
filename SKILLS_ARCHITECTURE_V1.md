# Skills Architecture v1

## Status and purpose

This document is the approved architecture contract for the Skills-aware Project System foundation. A project Skill is a portable semantic workflow: it tells an AI agent how to perform a repeatable project process. It is never a second source of project truth.

The operating model is:

- **KNOWLEDGE** — canonical project state: what the project knows.
- **SKILLS** — reusable orchestration: how an agent performs bounded work.
- **AGENTS.md / PROJECT_RULES.md** — short permanent constitution and precedence.
- **SCHEMAS / POLICIES** — machine-readable structure, constraints and governance.
- **CLI / SCRIPTS** — deterministic execution, scope calculation and validation.
- **MCP / PLUGINS** — external capabilities, never implicit authority.
- **AI MODEL** — semantic reasoning and editing inside prepared bounds.
- **HUMAN** — authority for meaning-changing approval.

The fundamental invariant is: **Skills are orchestration, never a second source of truth.** Deterministic tooling decides scope and validity. Semantic editors perform work inside that scope. Humans approve meaning-changing decisions.

## Canonical layout

Project-local portable Skill entrypoints live at:

```text
.agents/skills/<skill-name>/SKILL.md
```

Machine metadata lives separately at:

```text
.project/skills.yaml
```

Distribution assets live in:

```text
project_cli/project_system_assets/skills/
project_cli/project_system_assets/schemas/skills.schema.json
```

`SKILL.md` frontmatter contains only `name` and `description`. Project System write ceilings and machine capabilities live only in the registry. Core v1 has no Skill dependencies and no Skill-local executable scripts.

## Required Skills

Every activated v1 project installs these Skills:

| Skill | Responsibility | Explicit non-responsibility |
|---|---|---|
| `knowledge-sync` | Synchronize already approved meaning into scoped canonical objects and narratives. | Approval, brainstorming, code implementation, commit, push, release. |
| `decision-management` | Preserve proposal/question/decision lifecycle, approval evidence and replacement traceability. | Choosing or self-approving the decision. |
| `requirements-management` | Maintain approved requirements, feature boundaries and traceability. | Product prioritization, architecture selection or implementation. |
| `architecture-impact` | Analyze affected architecture, data, integration, security, privacy and operations. | Approving architecture/security or implementing the design. |
| `implementation-plan` | Convert approved scope into a bounded implementation and test plan. | Canonical writes or code changes. |
| `project-validation` | Run and interpret deterministic validation, generation and health checks. | Claiming semantic correctness or silently repairing meaning. |
| `release-check` | Perform bounded readiness, test, packaging and Git-state checks. | Version mutation, commit, tag, push, publish or release. |

`design-handoff` is additionally required when an enabled configured design integration, including the Google Workspace design bridge, is active. It coordinates Git knowledge, referenced visual truth and designer proposals without making external input canonical.

Research, generic code review, testing, security review and framework-specific workflows may be user-level Skills. They must not be hidden correctness dependencies of the project.

## Triggering and progressive disclosure

The single-line `description` states positive use conditions and important exclusions so a semantic agent can select a Skill without loading its body. A prepared command may also select repeatable `--skill NAME` values explicitly. Only selected `SKILL.md` documents enter a context pack; the entire catalog is never loaded by default.

Natural-language trigger quality is not deterministic. `tests/fixtures/skills/trigger-cases.yaml` is a reference/evaluation corpus for a future model-backed eval runner, not an automated v0.12 release gate. Deterministic tests cover identity, integrity, scope and governance behavior.

## Registry contract

The strict v1 registry has this shape:

```yaml
schema_version: 1
profile: project-system-skills-v1
skills:
  knowledge-sync:
    max_writes:
      - knowledge/**
      - docs/**
    capabilities:
      - project.context
      - project.validate
```

`max_writes` is a semantic/project write ceiling, not a grant by itself. `capabilities` contains only allowlisted machine capability IDs. A capability is not a literal shell command and cannot carry an arbitrary executable path. Provider mutation still requires the existing task, policy and human authorization.

The schema deliberately has no `enabled`, `depends_on`, arbitrary `commands`, commit, push, tag or release capability.

## Permission and output model

For one selected Skill:

```text
effective semantic write scope =
    Skill max_writes
    intersect current task or SYNC allowed write set
    intersect governance authorization
```

For several Skills, the result is the union of each Skill's independently valid intersection. Authority fragments from different Skills cannot be composed to authorize a path that no one Skill fully authorizes.

The prepared manifest separates:

```yaml
task_write_scope:
  canonical: []
  derived: []
```

- `skill.max_writes` contains semantic/project paths only.
- `task_write_scope.canonical` is the task-authorized non-derived scope.
- `task_write_scope.derived` lists outputs owned by specific deterministic commands.
- `.generated/**` is never Skill write authority and never enters the canonical SYNC `allowed_write_set`.

Forbidden Skill write roots are `.git/**`, `.github/**`, `.project/**`, `.agents/**`, `.generated/**`, `history/**` and `project.yaml`.

## Capability model

Known IDs are implemented by the runtime and schema, including context, impact, validation, generation, health, PR preparation, deterministic SYNC lifecycle preparation and the bounded Google Workspace status/sync surfaces. Selecting a Skill does not execute a command. The runtime maps an authorized capability to an existing deterministic operation; it never evaluates registry text as a shell command.

## Context, task and bootstrap packs

`project context`, `project task` and the existing knowledge `project bootstrap` accept repeatable `--skill NAME`. For activated projects, deterministic task defaults may select relevant installed Skills. New manifests bind:

- selected Skill name, project-relative path and SHA-256;
- registry SHA-256;
- canonical and derived task scopes;
- effective semantic scope and per-path Skill authorization.

`context.md` contains only selected Skill entrypoints. Global user Skills are outside the project and outside its integrity contract.

`project bootstrap` retains its v0.11 meaning: it prepares a knowledge-bootstrap context pack. It is not the Skills installer. Project setup/migration uses `project skills install`.

## SYNC lifecycle integration

No SYNC REQUEST or SYNC PACK input schema change is required. For an activated Skills project, new `plan.json` and `manifest.json` bind selected Skills, registry hash, per-path authorization and effective scope. `project sync verify` rechecks those hashes and confirms that every canonical allowed path remains authorized before retaining the existing Git-scope checks and `validate -> generate -> validate` pipeline. Verification is deterministic structural evidence, not semantic approval.

Finalization consumes the successful verification for the same evidence and exact working-tree fingerprint. Proposal and unresolved changes still create no canonical writes. Legacy v0.11 artifacts with no Skills fields remain supported; partially present Skills evidence fails closed.

## Knowledge, policies and external systems

- `knowledge/**` owns atomic lifecycle truth; a Skill only orchestrates authorized edits.
- `docs/**` explains current intent; it does not become a procedure registry.
- `project.yaml` activates the layer with `tooling.skills_schema_version: 1` and enables conditional integrations.
- `.project/policies/**` remains the governance and impact boundary.
- JSON schemas own machine shape and enums.
- `history/**` is excluded from Skill write ceilings and normal context.
- `.generated/**` contains disposable deterministic evidence and views.
- Google Designer rows remain immutable proposals. Figma remains referenced visual truth where configured. Neither becomes canonical through Skill selection.

The human-readable `08_WORKFLOW.md`, `09_DOCUMENTATION_PROCESS.md`, `10_DECISION_PROCESS.md` and `operations/DEVELOPMENT_PROCESS.md` remain maps and explanations. They may point to Skills and deterministic commands but do not copy Skill steps, schema fields, capability enums or permission matrices.

## Deterministic validation and path safety

`project skills validate`, and normal `project validate`, check:

- strict YAML, duplicate-key and alias rejection, schema/profile/version;
- activation-marker consistency and required/conditional Skills;
- normalized lowercase-hyphen names and case-insensitive duplicates;
- directory, registry and frontmatter identity;
- non-empty one-line descriptions;
- registered, missing and unregistered directories;
- allowed resources and contained local references;
- allowlisted capabilities and normalized write patterns;
- forbidden write roots and history prohibition;
- lexical/resolved containment and symlink/junction/reparse rejection;
- packaged catalog/schema/template completeness.

Validation does not attempt to prove wording quality or semantic trigger fitness.

## Initialization and migration

New Skills-aware projects receive the core entrypoints, strict registry and activation marker directly from installed package assets. Conditional `design-handoff` is materialized only when the relevant integration is enabled at setup time; later configuration changes make its absence a validation error until installation is rerun.

Existing projects use:

```text
project skills install          # dry-run exact plan
project skills install --apply  # controlled setup mutation
```

Apply requires a clean Git worktree outside disposable generated output, validates every planned path immediately before writing, and rolls back written files if final layer validation fails. Known stock v0.11 `AGENTS.md` and `PROJECT_RULES.md` are upgraded only after exact SHA-256 identity proof. Divergent files are never overwritten and require a manual merge. Migration does not change `knowledge/**`, `docs/**`, `history/**`, product truth or external resources.

Legacy v0.11 projects without an activation marker remain valid with a migration warning. Once activated, a missing or damaged registry is an error and cannot silently downgrade to legacy mode. A project pinned to CLI 0.12 or later must activate Skills v1.

## Packaging boundary

The wheel and source distribution contain this contract, the registry schema, catalog, eight Skill templates, known-stock migration identities and runtime validator. `project init` and migration therefore work from an installed distribution without a source checkout. Validation checks contracts and local integrity; intentionally customized valid project Skills need not remain byte-identical to packaged templates.

## v0.12 boundary and limitations

The v0.12 foundation provides materialization, validation, explicit/default selection, evidence binding, permission intersection, SYNC verification/finalization binding, generation views, migration and installed-package completeness. It does not provide an LLM router, semantic trigger scoring, autonomous edits, autonomous approval, arbitrary execution, global-Skill management or external provider authority.

Release metadata and release actions remain a separate owner-authorized task.
