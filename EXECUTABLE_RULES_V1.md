# Executable Rules & Evidence Architecture v1

## Status

Architecture contract for the Project System executable-rules foundation.
This document defines boundaries and contracts only; it does not enable enforcement.

## Core principle

Rules that can be enforced should be enforced.

Rules that cannot be deterministically enforced must explicitly require AI or human verification rather than being treated as implicitly satisfied.

Project System fails closed when required assurance cannot be proven.

## Authority layers

Executable Rules v1 distinguishes three layers:

### System Invariants

Packaged CLI/runtime protections such as repository containment, safe paths, artifact integrity, schema integrity, Skill authority boundaries, and approval-identity limitations.
System Invariants are not project-editable and cannot be disabled or waived by project policy.

### Project Rules

Project-owned machine-governed constraints. Their canonical source is `.project/policies/rules.yaml`.

### Task Obligations

Contextual requirements derived from future Task Specifications, active Project Rules, project state, and risk classification.
They are evaluation inputs, not a second canonical rule registry.
Risk may add obligations or increase assurance, but must never weaken an active Project Rule.

## Canonical storage

The Rules layer extends the existing `.project/policies/**` boundary.

Canonical policy layout:

```text
.project/policies/
  governance.yaml
  impact.yaml
  retrieval.yaml
  rules.yaml
  rule_exceptions.yaml
```

`rules.yaml` is the Single Source of Truth for Project Rule definitions.

`rule_exceptions.yaml` is the Single Source of Truth for governed exceptions. It never redefines a rule.

`project.yaml` activates Rules v1 through:

```yaml
tooling:
  rules_schema_version: 1
```

There is no generic `rules: false` escape hatch after activation.

Generated human-readable rule indexes and evidence are noncanonical derived state.

`AGENTS.md`, `PROJECT_RULES.md`, docs, and Skills may explain or reference the mechanism but must not duplicate the active rule registry.

## Rule registry

Rules v1 uses one registry rather than one file per rule:

```yaml
schema_version: 1
profile: project-system-rules-v1
rules: {}
```

Each rule may define identity, lifecycle status, category, description, verification, scope, enforcement, traceability, and exception policy.

Project YAML must never contain arbitrary shell commands, executable paths, inline Python, or a general-purpose expression language.

## Rule identity

Rule IDs are stable project-policy identifiers such as `ARCH-001`, `SEC-004`, and `TEST-007`.

Rule IDs must be unique within the registry and must not collide with canonical knowledge-object IDs.

Meaning-preserving edits may retain the same ID.
A materially different constraint should receive a new ID and deprecate or supersede the old rule.

Every evaluated rule is bound to a deterministic rule-definition hash in evidence.
Changing a rule definition therefore makes older evidence stale for the changed definition.

Git remains the version history; Project System does not create one history file per rule revision.

## Rule lifecycle

Supported lifecycle states are:

```text
draft
active
deprecated
```

There is intentionally no generic `disabled` state.

`draft` rules are defined but not enforced.
`active` rules participate at configured checkpoints.
`deprecated` rules remain visible for traceability and may identify a replacement.

## Categories

Rules v1 supports these categories:

```text
architecture
repository
dependency
security
requirement
testing
process
```

Categories organize policy and reporting. They do not select checker implementation by themselves.

## Verification methods

Every rule declares exactly one verification method:

```text
deterministic
ai
human
```

### deterministic

A deterministic rule references one allowlisted checker implemented by Project System.
It may produce machine-verifiable PASS or FAIL evidence.

### ai

An AI-verifiable rule requires a future structured AI attestation bound to the rule definition, evaluation context, project state, and reviewer identity.

Without valid attestation its result is PENDING, never PASS.
AI evidence must never be represented as deterministic evidence.

### human

A human-verifiable rule requires governed human review or decision evidence.

Local approval metadata may prove structural completeness but does not prove the real identity of the approver.
Strong human identity and review enforcement remain a hosting-platform responsibility where required.

Without required human evidence the result is PENDING.

## Checker model

Deterministic rules reference allowlisted checker IDs.

Examples:

```text
repository.required_path
repository.forbidden_path
knowledge.required_field
architecture.dependency_boundary
```

Checker implementations belong to the packaged Project System runtime.
Checker-specific parameters are validated against checker-specific contracts.

Rule definitions never contain shell commands, executable paths, inline Python, or arbitrary executable expressions.

Future language-specific analysis must use bounded Fact Providers or adapters that produce normalized facts for generic checkers.

Example:

```text
Dart analyzer
    |
    v
normalized import/dependency facts
    |
    v
architecture.dependency_boundary
```

### Fact Provider contract

Fact Providers are packaged, allowlisted deterministic adapters. They inspect
bounded source inputs and return normalized language-neutral facts; they do not
contain architecture policy, choose Rule outcomes, or execute project-supplied
commands or code. Generic checkers consume those facts in a later stage.

Every provider has a stable packaged ID and implementation version. A provider
result carries that identity, the sorted inspected source paths, a canonical
deduplicated fact tuple, and `fact_set_sha256`. The hash is calculated from the
normalized fact set and is independent of absolute checkout location and
filesystem enumeration order.

Stage 8A introduces provider `dart.imports`, version `1`. It lexically extracts
Dart `import`, `export`, `part`, and URI-form `part of` dependency directives,
including all conditional import/export URI branches, while excluding comments
and arbitrary string contents. Named `part of library.name;` declarations are
not resolved heuristically and fail closed. Deferred imports require the valid
`deferred as prefix` form. Dependency facts retain the source path, directive,
original URI, and one normalized target kind:

```text
project    repository-relative target path
package    external Dart package identity
dart_sdk   Dart SDK URI
```

An import of the root package declared by a safely parsed `pubspec.yaml`
normalizes to its `lib/**` project path. Without a valid package name, package
URIs remain external rather than being guessed as internal. Declared dependency
targets need not exist for a fact to be emitted.

Provider evaluation follows the Stage 7 path boundary without depending on Rule
Engine internals: `evaluation_paths=None` performs deterministic project-wide
Dart discovery, `evaluation_paths=()` inspects no sources, and a non-empty
bounded set inspects only existing `.dart` files in that canonical set.
Project-wide discovery excludes `.git/**`, `.generated/**`, `build/**`, and
standard Dart tool caches. It never follows symlinks or reparse points.

Unsafe containment, ambiguous reparse state, unreadable or invalid UTF-8 source,
malformed dependency URIs, unsafe `pubspec.yaml`, and unsupported directive
syntax fail closed. Fact Providers remain policy-free and cannot decide whether
one normalized dependency is allowed.

### Architecture dependency boundary checker

Stage 8B registers packaged checker `architecture.dependency_boundary`, version
`1`. It consumes normalized Fact Provider output and does not parse Dart or
contain language-specific import rules. Its strict parameters are:

```yaml
provider: dart.imports
source_paths:
  - lib/domain/**
forbidden_target_paths:
  - lib/presentation/**
```

Both path lists are non-empty canonical repository-relative anchored POSIX glob
patterns using the shared Rule scope matcher. Unknown providers, parameters, or
unsafe patterns are rejected. A violation exists only when a normalized fact has
`target_kind: project`, its source matches `source_paths`, and its target matches
`forbidden_target_paths`. External packages and Dart SDK dependencies do not
violate project path boundaries. Every normalized dependency directive emitted
by the selected provider participates.

Project-wide validation evaluates the provider project-wide. A bounded
Evaluation Context normally passes its exact changed paths to the provider, so
an unrelated change does not scan existing source files. Provider-owned global
input metadata can invalidate that bounded universe: for `dart.imports`, a
`pubspec.yaml` change deliberately causes project-wide provider evaluation
because package ownership may change.

Governance inputs that can change architecture meaning also invalidate bounded
dependency evaluation. A bounded change to either
`.project/policies/rules.yaml` or
`.project/policies/rule_exceptions.yaml` causes project-wide provider
evaluation so a new or changed boundary, waiver, revocation, or exception
definition cannot falsely pass without inspecting existing source.

Temporary architecture exceptions are also time-dependent even when no
governance file changes. When an active temporary exception targets an
`architecture.dependency_boundary` Rule, validation records that Rule ID in
`complete_evaluation_rule_ids`. The real bounded `evaluation_paths` remain
unchanged, but the Rule Engine derives a project-wide checker context for that
specific Rule so expiry cannot hide an existing raw architecture failure.

Result evidence records `provider_evaluation_mode` as `bounded`,
`project_wide`, or `project_wide_invalidation`.

In v1, dependency-boundary Rules must not declare Rule-level `scope`. The Rule
registry rejects that unsafe combination because Stage 7 applicability filtering
would run before provider invalidation. `verification.parameters.source_paths`
is the only dependency-source selector.

Violations record normalized source path, directive, target path, and original
target URI in deterministic order. Governed path-scoped exceptions apply to the
violating source file only, because that file owns the dependency declaration.
Provider identity/version, normalized fact-set hash, inspected sources,
evaluation mode, selectors, and complete violations remain bound into normal
Rule Evidence. A provider or result assurance failure is `ERROR`, never `PASS`.

Stage 8B completes the first language-backed architecture enforcement vertical
slice through the existing Rule Engine, exception, Evidence, project validation,
and bounded SYNC validation paths. It does not add a default architecture policy
to initialized or existing projects.

## Rule outcomes

Raw evaluation status is one of:

```text
PASS
FAIL
ERROR
NOT_APPLICABLE
PENDING
```

PASS means the required verification succeeded.
FAIL means the checker ran successfully and found a rule violation.
ERROR means required evaluation could not be completed reliably.
NOT_APPLICABLE means the rule does not apply to the resolved evaluation context.
PENDING means required AI or human verification is not yet satisfied.

ERROR is not equivalent to FAIL and must fail closed at a required gate.

After governed exception resolution, an effective result may additionally be:

```text
WAIVED
```

Evidence preserves both raw and effective status.

## Severity

Rules reuse the existing Project System severity vocabulary:

```text
BLOCKING
ERROR
WARNING
INFO
```

At a required gate, BLOCKING and ERROR violations prevent successful completion.

A checker execution or integrity ERROR cannot be waived.
WARNING and INFO remain visible in evidence and reports without becoming silent successes.

## Evaluation Context

The Rule Engine evaluates rules against an explicit Evaluation Context rather than arbitrary ambient state.

The context may contain:

```text
project identity
Git HEAD
base commit when applicable
changed paths
canonical object graph
project configuration
current checkpoint
task specification when available
task allowed and forbidden paths
change class
risk level
expected domains
acceptance criteria
collected facts
```

Not every field is required in the first implementation.

The context is intentionally extensible so Task Specification and Risk Engine support can be added without redesigning rule identity or evidence.

Rule applicability and resolved scope are derived from this explicit context.

### Project-wide and bounded path evaluation

Stage 7 adds `evaluation_paths` to the Evaluation Context without introducing a
Task Specification. Its values are:

```text
null / None   project-wide, unbounded evaluation
[] / ()       bounded evaluation with no affected canonical paths
[paths...]    bounded evaluation over that exact canonical path set
```

Concrete evaluation paths are canonical repository-relative POSIX paths. They
are deduplicated and sorted before evaluation and evidence hashing. Absolute
paths, traversal, `.git/**`, backslashes, drive syntax, NUL, glob syntax, and
noncanonical path forms fail closed. `None` and an empty bounded set are
semantically distinct.

Checkpoint applicability is evaluated before path scope. A configured Rule
without `scope` remains applicable in any bounded context. A Rule with
`scope.paths` remains applicable in project-wide context; in bounded context it
is applicable only when at least one evaluation path matches at least one scope
pattern. Otherwise it produces `NOT_APPLICABLE` with reason
`scope_no_intersection`, and its checker is not invoked.

Rule scope and governed exception scope share one repository-root-anchored glob
matcher. `*`, `?`, and character classes match within one path segment. A
segment that is exactly `**` matches zero or more complete segments, and the
whole path must be consumed. Thus `docs/*.md` excludes nested files while
`docs/**/*.md` includes both direct and nested Markdown files.

`resolved_scope` continues to contain the declared Rule scope patterns, not the
matched concrete paths. The canonical bounded path set is bound through the
Evaluation Context and `evaluation_context_sha256`, so it also changes the
common Rule Evidence fingerprint.

Stage 8B extends the Evaluation Context with
`complete_evaluation_rule_ids`, a sorted deduplicated tuple of Rules that must
receive complete checker evaluation despite a bounded changed-path set. This
obligation is distinct from `evaluation_paths`: it does not rewrite what
actually changed. When non-empty it is also bound into
`evaluation_context_sha256`, so adding or removing a complete-evaluation
obligation necessarily changes the common Rule Evidence fingerprint.

Stage 7 does not introduce Task Specifications, task lifecycle or IDs, Task
Obligations, risk classification, or a Risk Engine. Those remain future stages.

## Checkpoints

Rules v1 reserves these checkpoint identities:

```text
project_validate
task_verify
sync_verify
```

`project_validate` remains the canonical project-wide validation gate and the command used by CI.

`task_verify` is reserved for future Task Specification completion verification.

`sync_verify` integrates executable rules into the existing deterministic SYNC verification lifecycle.

CI must invoke Project System rather than implement a separate CI-only rule engine.

## Evidence model

Every rule evaluation produces normalized machine-readable evidence even when the caller does not persist it.

Evidence is derived state and never canonical project truth.

An evidence run binds at least:

```text
project ID
current Git HEAD
base commit when applicable
CLI version
rules registry hash
exception registry hash
evaluation-context fingerprint
rule definition hashes
checker IDs and checker versions
raw results
effective results
resolved scopes
failure details
artifact references
applied exception IDs
```

An evidence record may additionally contain volatile presentation metadata such as `evaluated_at`.

Stable evidence fingerprints must be calculated from stable evaluation identity and results, not from volatile presentation fields.

Per-rule evidence conceptually contains:

```text
rule_id
rule_hash
verification_method
checker
checker_version
severity
resolved_scope
raw_status
effective_status
details
artifacts
exception_id
failure_reason
```

Therefore a statement such as `ARCH-001 PASS` always refers to a specific rule definition, checker implementation, evaluation context, and project state.

Old evidence must not be silently reused when any bound identity changes.

## Evidence persistence

The Rule Engine owns one evidence contract. Callers may persist or embed it differently.

```text
project validate     -> gate output and optional persisted evidence
project rules check  -> diagnostics, JSON, or generated evidence
project sync verify  -> rule evidence bound into verification.json
CI                   -> machine-readable CI artifact
```

SYNC is a consumer of the common Rule Evidence model, not a second evidence implementation.

Generated evidence may live under `.generated/evidence/**` and remains disposable derived state.

## Exceptions

Exceptions are explicit governed records, never boolean rule disabling.

Canonical source:

```text
.project/policies/rule_exceptions.yaml
```

Conceptual registry shape:

```yaml
schema_version: 1
profile: project-system-rule-exceptions-v1
exceptions: {}
```

Every exception must identify the affected rule, explicit scope, reason, approval metadata, and a linked Decision.

Temporary exceptions require expiration.
Expired or revoked exceptions remain auditable and do not apply.

A valid exception may transform:

```text
FAIL -> WAIVED
```

It must never transform:

```text
ERROR -> WAIVED
```

System Invariants cannot be waived by project exceptions.

Local validation checks approval metadata structurally and does not claim that `approved_by` proves real human identity.

## Traceability

Rules may reference existing canonical knowledge objects and canonical docs.

A Decision should carry the human rationale for introducing, changing, or waiving a significant rule.

The rule registry contains operational machine policy needed for evaluation and must not duplicate full decision rationale.

## Integration with existing Project System layers

### Schemas

Existing JSON schemas remain System Invariants.
Existing schema validation is not rewritten into Project Rules.

### Policies

`.project/policies/**` remains the canonical policy boundary.
Executable Rules extend this existing layer rather than creating a parallel policy tree.

### Skills

Skills describe how semantic work is performed.
Rules describe what resulting project state must satisfy.

A Skill cannot override, waive, mutate, or reinterpret a Rule verdict.
Existing Skill write ceilings continue to protect `.project/**`.

### AGENTS.md and PROJECT_RULES.md

These documents should contain only stable precedence principles and pointers to canonical rule registries.
They must not manually duplicate active rule definitions.

### project.yaml

`project.yaml` contains durable Rules-layer activation and version metadata only.
It does not contain individual Project Rule definitions.

### project validate

`project validate` remains the primary project-wide gate.
Rule evaluation is integrated into existing validation rather than creating a second validation universe.

### Generation

Generation may later create derived projections such as:

```text
.generated/indexes/RULES.md
.generated/reports/RULE_EVIDENCE.md
```

These remain noncanonical and disposable.

### SYNC

Existing SYNC already provides integrity, scope, Skill evidence, validation evidence, and exact-state fingerprints.

`sync verify` evaluates the Rule Engine at the `sync_verify` checkpoint during its second validation pass. This evaluation is bounded by the exact final scope-safe `actual_changed_canonical_paths`; an empty canonical change set is an empty bounded evaluation. Its successful report binds the common Rule Evidence payload, including registry and rule hashes, exception identity, raw and effective results, bounded Evaluation Context, and the Evidence fingerprint, to the exact verified working-tree fingerprint. The same binding is retained in a scope-safe failed validation report so the failure remains auditable.

Finalization validates the stored binding and re-evaluates `sync_verify` Rules against the same base commit and persisted verified canonical path set before dry-run preparation, first commit, first commit-and-push, or reviewed-no-change completion. Any Rule Evidence drift, including an expired or revoked exception, makes verification stale and requires `project sync verify` again. A retry of an already recorded commit or push does not re-evaluate current Rules because the committed canonical bytes are already fixed.

Completed terminal bindings may carry the Rule Evidence fingerprint. New pushed and reviewed-no-change outcomes emit it; rejected and abandoned outcomes use `null`. The field remains optional so terminal records created before this binding existed remain schema-valid.

Rule Evidence does not grant semantic approval. Human review remains authoritative for meaning-changing changes, and no separate canonical Rules result is created outside the existing validation, SYNC report, and terminal-binding lifecycle.

### GitHub and CI

CI continues to invoke Project System validation.
No CI-only implementation of Project Rules is introduced.

Hosting-platform protection remains responsible for strong human-review enforcement where required.

## Backward compatibility and migration

Existing projects without `tooling.rules_schema_version` remain legacy-valid during migration.

New projects receive Rules v1 after the Rules layer is implemented.

Existing projects migrate through an explicit dry-run-first operation rather than silent mutation.

Migration must preserve:

```text
knowledge/**
docs/**
existing policy meaning
Skills
SYNC artifacts
external-system bindings
```

Custom policy files must never be silently overwritten.

Once `tooling.rules_schema_version` activates Rules v1, a missing or invalid rules registry is a validation error and must not silently downgrade to legacy behavior.

## Initial checker scope

The first implementation intentionally starts with a small deterministic checker catalog:

```text
repository.required_path
repository.forbidden_path
knowledge.required_field
```

Architecture, dependency, testing, and security adapters follow only after the core Rule Engine and Evidence pipeline are proven.

The first language-specific architecture adapter should target Dart/Flutter.
It should normalize imports and dependency relationships into facts consumed by the generic architecture boundary checker.

## Task Specification and Risk readiness

A future Task Specification may contribute:

```text
allowed paths
forbidden paths
expected changed domains
required checks
risk hints
acceptance criteria
invariants
```

Risk classification may add obligations or increase required assurance.
It must never suppress active Project Rules.

Future flow:

```text
Task Specification
+ Project Rules
+ Project State
        |
        v
Evaluation Context
        |
        v
Risk and Applicability Resolution
        |
        v
Task Obligations
        |
        v
Implementation
        |
        v
Verification
        |
        v
Evidence
        |
        v
Completion Gate
```

## Bootstrap readiness

Future one-command bootstrap may inspect an existing codebase, documentation, and connected design sources and propose candidate rules.

AI-inferred rules must never become active canonical Project Rules solely because an AI inferred them.

Bootstrap must distinguish discovered facts, inferred candidates, approved canonical rules, and unknowns.

Meaning-changing inferred policy requires human approval before activation.

## Explicit v1 non-goals

Rules v1 does not initially provide:

```text
arbitrary policy scripting
arbitrary project-configured commands
automatic AI attestation
cryptographic human identity proof
full Task Specification Engine
full Risk Engine
language analyzers for every stack
automatic migration of every existing validator into rules
one file per rule
automatic exception creation
automatic rule disabling
```

Existing deterministic Project System invariants remain in place throughout migration.

## Implementation sequence

```text
1. Rule contract, schemas, registries, activation and validation
2. Rule Engine core with a small built-in checker registry
3. Evidence v1
4. Integration into project validate and CI gate
5. Governed exception resolution
6. SYNC verification and finalization evidence binding
7. Bounded Rule Evaluation Context and evaluation paths
8A. Fact Provider foundation and Dart/Flutter dependency fact extraction
8B. Generic architecture dependency-boundary checker
9. Code Verification Adapters
10. Code Quality Gates
11. Mutation and Regression Quality
12. Task Specification, Risk Engine, and Task Obligations
13. External Source Intake & Canonicalization
    13A. Source Capture & Provenance
    13B1. Source Representation & Evidence Anchors
    13B2. Semantic Extraction & Proposal Sealing
        13B2a. Extraction Contract & Deterministic Sealer
        13B2b. Semantic Executor Integration
            13B2b1. Verified Extraction Pack
            13B2b2. Authorized Semantic Executor
    13B3. Independent Semantic Audit
    13C. Human Review & Canonical Apply
14. Bootstrap and end-to-end AI Development Gate
```

Each stage must preserve existing behavior, add focused tests, inspect the diff, and pass the complete Project System test suite before the next stage.

### Stage 9B: Dart test verification contract

`dart.test@1` is an allowlisted `code.verification` adapter with
`executes_project_code=True`. It invokes only `dart test --reporter json` through
the existing process runner, with `shell=False`, a 300-second parent timeout and
a 16 MiB limit per captured stream. Rule parameters accept only the adapter ID.

Without a deterministic dependency map, every non-empty bounded project change
escalates to `project_wide_invalidation`, including fixtures, documentation and
assets. Unbounded evaluation is `project_wide`. Both run the complete configured
suite. An empty bounded set returns `NOT_APPLICABLE` without execution. No test
selection heuristics or Rule scope are supported.

The JSON reporter protocol `0.1.x` is validated before classification. A failing
completion must agree with its preceding `error` events: `failure` requires only
TestFailure errors and `error` requires at least one non-TestFailure error.
Successful completion with an earlier error is malformed. Errors after successful
completion are supported and change the final outcome to failure. Malformed,
incomplete or inconsistent output, unsupported exit codes, timeout and execution
failure become Rule `ERROR`, never a waivable `FAIL`. Exit 0 must agree with
successful completion; exit 1 must agree with proved test failures. All-skipped,
all-hidden or empty completed runs return `NOT_APPLICABLE`; at least one executed,
non-hidden, non-skipped successful test is required for `PASS`.

Verification Adapter results now support an optional `semantic_sha256` contract,
required only for adapters whose packaged registry metadata declares
`uses_semantic_hash=True`. Such results retain raw `stdout_sha256` and
`stderr_sha256` as execution provenance at the adapter boundary. Their stable
`result_sha256` binds `semantic_sha256` instead of those volatile raw hashes.
Semantic Rule Evidence contains `semantic_sha256` and the normalized result,
excluding raw transcript hashes. Those hashes remain available in the returned
execution result, but are not persisted as part of deterministic Rule Evidence.
They do not prove semantic equality, and no durable execution transcript report
is introduced in Stage 9B.

The Dart semantic digest binds a versioned representation of reporter/runner
versions, sorted suite paths/platforms and the complete sorted test inventory:
suite, name, source coordinates, completion/final outcome, skipped/hidden state
and counts/classes of errors before/after completion. Duplicate semantic test
identities preserve multiplicity. Reporter IDs, PID, elapsed time, independent
event interleaving and raw print/error/stack text are excluded. Paths are contained
in the project root. Findings remain normalized, sorted and deduplicated.

Consumers reject missing/malformed semantic hashes, contract downgrades, raw
provenance injection into semantic Evidence, identity/version mismatches and
tampered result hashes. Legacy adapters, including `dart.analyze`, retain their
existing raw-hash result/Evidence contract unchanged. SYNC rechecks use the same
Evidence validation; no finalization bypass is introduced. Unreleased Stage 9B
artifacts using the previous raw-hash contract must be regenerated.

Remaining Stage 9B limits: project code runs with CLI privileges and no OS sandbox;
the parent timeout does not guarantee process-tree termination; environment and
toolchain fingerprinting remain incomplete. Flutter/pytest/npm, coverage and
selective dependency mapping are future stages.

### Stage 9C1: resolved dependency vulnerability verification

`osv.scan@1` is a packaged, cross-ecosystem `code.verification` adapter, not a
Dart-only checker. Registry metadata declares `executes_project_code=False`,
`uses_semantic_hash=True` and `uses_network=True`. `uses_network` is a strict
boolean defaulting to `False` for existing adapters. It is packaged capability
metadata, not a project parameter or a promise of network sandboxing. Existing
Dart adapter contracts are unchanged; no new result/Evidence fields are needed.

Supported exact filenames are `pubspec.lock`, `package-lock.json`,
`pnpm-lock.yaml`, `yarn.lock`, `bun.lock`, `uv.lock`, `poetry.lock`, `Pipfile.lock`,
`pdm.lock`, and `pylock.toml`. Discovery recursively returns sorted canonical
repository-relative regular files. It never follows symlinks/reparse points and
fails closed on unsafe/ambiguous paths or unreadable non-excluded directories.
Exclusions, matched case-insensitively, are `.git`, `.generated`, `.dart_tool`,
`.pub-cache`, `build`, `node_modules`, `.venv`, `venv`, `__pycache__`, `vendor`,
`dist`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.cache`, `.tox`, `.nox`,
and `coverage`. Excluded subtrees have no dependency authority in this adapter.
Manifests and `requirements.txt` are not vulnerability inputs in v1.

Without dependency mapping, every non-empty bounded project change escalates to
`project_wide_invalidation`; `None` runs project-wide. Empty bounded input, or
trusted discovery finding no supported lockfiles, is `NOT_APPLICABLE` without
launching OSV. Passing explicit lockfiles but receiving no packages is `ERROR`.

The adapter resolves an installed `osv-scanner` executable outside the project,
probes `--version`, and accepts only one strictly parsed stable version line
in the range **`>=2.3.3,<3.0.0`**. Prerelease/dev strings are rejected. The floor
binds complete inventory (`--all-packages` fixed in 2.3.0), `bun.lock` support
(2.3.2) and `pylock.toml` extraction (2.3.3); unsupported versions produce
`VerificationAdapterError`/Rule `ERROR` before scan, with no weaker fallback.
It runs from an adapter-owned temporary directory outside the project,
with an empty `osv-scanner.toml` override. Config and directory are cleaned on
success and failure; a project-controlled temporary root inside the project is
rejected before creation. The fixed scan argv is:

```text
<external-osv-scanner> scan source --format=json --all-packages --no-resolve
  --no-call-analysis=all --all-vulns --config <temporary-empty-config>
  -L <absolute-discovered-lockfile> ...
```

No directory scan, project flags, remediation, package manager, call analysis,
license policy or severity threshold is enabled. `--all-vulns` prevents
uncalled/unimportant vulnerability filtering from overriding the policy that
any known vulnerability fails. `shell=False`; the version probe has a 10-second
timeout and 64 KiB capture limit per stream; scan has a 300-second timeout and
16 MiB capture limit per stream. Supported binaries failing these fixed flags
produce execution errors rather than falling back to weaker scan semantics.
Comma-containing lockfile paths (including ancestors), parser delimiters,
control characters and noncanonical paths are rejected, not forwarded as flags.

JSON is untrusted: duplicate keys/nonstandard numeric constants, unknown result
envelopes, malformed inventories and contradictory groups are rejected. Every
source must be a discovered explicit lockfile, each source must appear once with
non-empty packages, and full discovered source coverage is required. Package
ecosystem/name/resolved-version and advisory/group IDs must be bounded normalized
identity tokens. Each advisory's semantic identity is its ID union its normalized
aliases. Group IDs must partition the full advisory ID set; a group's normalized
aliases must equal exactly the union of those advisory identities, including its
own IDs. No alias may belong to two distinct groups. Repeated equivalent groups
normalize deterministically; duplicate advisory IDs are accepted only with equal
semantic alias identities, otherwise `ERROR`. Contradictory/missing/extra aliases
are integrity `ERROR`, not vulnerability `FAIL`.

Upstream `PackageSource.experimental_pes` is recognized: empty lists are accepted,
non-empty signals are unsupported capability `ERROR`. Invented
`experimental_annotations` is rejected even when empty. Actual `PackageInfo`
fields are used: `image_origin_details` (only absent/null accepted),
`os_package_name` and `commit` (only absent/null/empty accepted), and `deprecated`
(only absent/boolean false accepted). Invented `image_origin` is rejected.
Deprecated-package scanning is not enabled and no flags are added.
Unsupported commit/image/license/generic finding structures fail closed. Advisory prose is
not evidence and is not copied into failure reasons or findings. File metadata
and discovery are rechecked around execution to detect observable input drift;
this is not an OS-level filesystem snapshot or adversarial race guarantee.

`PASS` requires complete package coverage, no known vulnerabilities and exit 0.
`FAIL` requires proved vulnerabilities and exit 1, with concrete source-lockfile
findings (`ERROR` severity, `osv.vulnerability`, bounded package/version/ID
messages). All other codes, including reserved result codes, 127 general error,
128 no packages and 129 network error, are `ERROR`, never waivable `FAIL`.
Missing executable, unsupported version, timeout, capture overflow, network or
parser failure also produce sanitized `VerificationAdapterError`/Rule `ERROR`.

Semantic schema v1 binds sorted discovered lockfile inventory, complete sorted
package inventory (source/ecosystem/name/version, preserving multiplicity) and
vulnerability IDs/group IDs/aliases per package. JSON/result/package/group order,
advisory prose/timestamps/references and stdout/stderr do not affect the semantic
digest. Raw execution hashes remain at the adapter boundary and are validated;
stable result/Evidence hashes use the existing semantic contract. OSV version,
normalized status and findings remain bound by the common result hash.

This is mutable, network-backed external security state: new vulnerability
identities between verify and finalize are meaningful Evidence drift. Normal
SYNC rechecks remain authoritative; there is no finalization bypass or forever
cached PASS. Governed Project System Rule Exceptions remain the only policy
owner. Project-local OSV ignores/configuration are overridden, not authoritative.

Stage 9C1 does not install OSV, cryptographically attest tool binaries, manage
offline databases, scan arbitrary manifests/version ranges, upgrade dependencies,
perform secret/SAST/malware/license/container scanning, generate SBOMs, apply
severity thresholds or selective dependency mapping. Tool/environment trust,
OSV API availability, unsupported/unresolvable lockfile entries and parser
compatibility remain operational constraints. Passing a lockfile scan proves
known-vulnerability lookup for resolved inventory, not complete software security.

Upstream contracts: [OSV v2 source command](https://github.com/google/osv-scanner/blob/v2.3.3/cmd/osv-scanner/scan/source/command.go),
[fixed flags](https://github.com/google/osv-scanner/blob/v2.3.3/cmd/osv-scanner/internal/helper/flags.go),
[JSON models](https://github.com/google/osv-scanner/blob/v2.3.3/pkg/models/results.go),
[config override](https://google.github.io/osv-scanner/configuration/) and
[output/return codes](https://google.github.io/osv-scanner/output/).

### Stage 9C2: dependency verification integrity hardening

Stage 9C2A hardens `osv.scan@1` inside the existing adapter. The adapter version,
successful result contract, Rule Evidence and semantic schema v1 are unchanged;
there are no new project parameters, scanners, flags or lifecycle bypasses.

**Content-bound inputs.** Each lockfile state contains its canonical relative
path, portable filesystem identity (`st_dev`, `st_ino` as available on the host),
size, nanosecond mtime and streamed SHA-256 of its bytes. T0 follows authoritative
discovery; T1 follows the version probe and precedes scan; T2 follows scan and
precedes acceptance of output. T1/T2 repeat authoritative discovery of the whole
supported inventory, not just the original paths. Added/removed lockfiles,
replacement, unsafe containment/reparse changes and content drift, including
same-size mutation with restored mtime, produce `VerificationAdapterError`/Rule
`ERROR`, never a waivable vulnerability `FAIL`.

Hashing is itself a fail-closed boundary: safe ancestor/path lstat before open;
binary read-only no-follow open; handle fstat must identify the same regular,
non-reparse object as the path observation; stream in fixed 1 MiB chunks; repeat
handle fstat and safe path lstat after reading. Identity/size/mtime must agree
throughout and the number of bytes read must equal the observed size. Handles
are closed on success and error. POSIX uses `O_NOFOLLOW` and `O_NONBLOCK` (to avoid
a raced FIFO blocking before its rejection). Windows uses `CreateFileW` with
`FILE_FLAG_OPEN_REPARSE_POINT` and rejects reparse handle attributes before any
content read; handles/descriptors are non-inheritable. No unsafe open fallback
is provided if the necessary primitive is unavailable. Ancestor components are
checked before/after reading; this is not atomic path resolution or a snapshot.

**Executable consistency.** PATH lookup and canonical external executable
resolution remain unchanged. The canonical regular executable's path,
identity/size/mtime and streamed binary SHA-256 are compared at E0 before version
probe, E1 immediately after it, E2 immediately before scan and E3 immediately
after scan. Observable executable content or identity drift is `ERROR`.
This establishes that *the same observable executable was used throughout this
verification run*. It does not establish authentic/trusted publisher software.
There is no signature/publisher verification, allowlisted digest, installation
or cryptographic binary authenticity attestation.

**Temporary policy consistency.** The adapter-owned temporary directory is
resolved outside the project, must remain a real non-reparse directory and
retains its observed directory identity. Config is exclusively created by the
adapter, never overwrites a pre-existing file, and must be a regular non-reparse
file whose authoritative content is exactly empty bytes. Config identity, size,
mtime and empty-content digest are compared at C0 after creation, C1 before scan
and C2 after scan. Change/replacement/deletion is `ERROR`. Explicit `--config`
still removes project-local OSV configuration/ignores from authority. Temporary
cleanup remains active on success, parser/version error, timeout, process
exception and integrity error while directory ownership is intact. Cleanup
checks saved directory identity before removal, refuses observed replacements
and does not leave an unconditional finalizer which might delete a foreign
directory later. No permanent project artifacts are created.

**Exact groups.** The adapter reconstructs connected components of advisory
identities `{id} union normalized aliases` independently of reported OSV groups.
Two advisories are adjacent when their identities intersect; transitive overlap
therefore connects them too. Each expected group has sorted component advisory
IDs and the sorted union of their identities. The normalized reported group set
must equal exactly these components. Splitting connected advisories, merging
disconnected ones, missing/extra aliases or IDs and conflicting duplicate
identities are `ERROR`. Established equivalent duplicate advisory/group rows
still normalize deterministically and ordering/prose remain non-semantic.

**Runtime integrity is not semantic Evidence.** Runtime hashes and metadata
decide whether a result may be accepted. None of lockfile/binary/config content
hashes, inode/mtime, temp paths or timestamps enters `semantic_sha256` or Rule
Evidence. The digest continues to bind only lockfile inventory, package inventory
and resolved versions, advisory IDs and normalized groups/aliases. Stable runs
establishing the same semantic result retain the same semantic/result hash even
when runtime state differs between runs. `NOT_APPLICABLE`, `PASS`, vulnerability
`FAIL` and infrastructure/integrity `ERROR` semantics remain those of Stage 9C1.

**Stage 9C2A limits.** This is not an OS-level snapshot, filesystem
transaction, OS sandbox or binary authenticity attestation. Transient mutation
fully restored between observable checks can remain undetected, including
concurrent changes during a streamed read which leave no observable metadata
contradiction. Portable identity fields depend on host filesystem support;
hashing costs are linear in input/binary bytes and are not given a separate
wall-clock deadline. Cleanup cannot guarantee recovery of artifacts moved away
by an external actor, and fails closed on observed unsafe paths rather than
following them. OSV remains external and network-backed, without offline DB,
network cache, Evidence TTL, remediation or selective dependency mapping.

#### Stage 9C2B: mutable network state / SYNC Rule Evidence revalidation

`sync_verify` persists common Rule Evidence at the `sync_verify` checkpoint,
bound to the exact verified working-tree fingerprint. Before preparing or
creating a new SYNC commit, `sync_finalize` re-evaluates current Rules through
that same checkpoint, with the same base commit and changed canonical paths.
An applicable `code.verification` Rule using `osv.scan@1` therefore executes a
fresh OSV scan; stored `PASS` is not used as the current verification result.
The existing retry path for an already proven SYNC commit does not reopen its
historical verification or authorize a different commit.

Working-tree identity and external vulnerability state are different axes.
Unchanged file bytes, Git HEAD/index and verification fingerprint do **not**
establish unchanged OSV state. Current `BLOCKING`/`ERROR` issues reject
finalization. Even when current issues are non-blocking, the freshly rebuilt
Rule Evidence binding must equal the verified binding exactly; otherwise a new
`project sync verify` is required. A valid new vulnerability is raw `FAIL` and
blocks stale finalization. `WARNING`/`INFO` enforcement does not bypass Evidence
drift. An existing governed exception may make a current vulnerability `WAIVED`,
but cannot retroactively make old `PASS` Evidence current. Execution/network
failure or an untrustworthy scan is raw/effective `ERROR`, not vulnerability
`FAIL`, cannot be waived and blocks regardless of enforcement severity. Raw
exception/transport text is not exposed in Rule failures or finalization reports.

Fresh execution does not imply that raw representations must be identical.
Already accepted JSON formatting, object/package/group/alias ordering, advisory
prose and stderr volatility are non-semantic: equivalent inventory and findings
retain the same `semantic_sha256`, `result_sha256` and Rule Evidence binding.
This does not relax the parser's inventory, exit-code or exact-group checks.
Accepted `tool_version` remains result-hash-bound; a changed tool version may
require new verification even when the semantic inventory is identical.

Integration coverage in `tests/test_sync_finalize.py` uses real isolated Git
repositories, `plan_sync`, `verify_sync`, `finalize_sync`, Rule evaluation and
the authoritative OSV parser. Only external OSV process transport is simulated;
checkpoint/Evidence observation delegates to real validation. Scan counts must
increase separately during verification and finalization, without assuming an
exact total. Tests exercise unchanged non-generated file bytes, Git changes,
HEAD/index and verified-state fingerprint under new vulnerabilities, network
exceptions/reserved network exit, non-blocking severity and governed waiver;
equivalent `PASS` allows dry-run preparation. No live scanner/network/push is
used, and finalization neither stages nor commits in these tests. Disposable
`.generated/**` reports are excluded from the repository identity comparison.

Freshness uses authoritative re-execution at the current checkpoint, not a TTL,
timestamp, database version, external-state identity or persistent result cache.
No freshness fields are added to successful adapter results or Evidence.
`uses_network=True` remains packaged `osv.scan@1` registry metadata, not a
Rule/project override, result field or Evidence field. Adapter version,
OSV semantic schema and `EVIDENCE_SCHEMA_VERSION` all remain `1`.

Stage 9C2 dependency verification integrity hardening is complete for
Stage 9 resolved dependency vulnerability verification. Completion is limited
to resolved dependency vulnerability verification, not all security verification.
Re-execution is an observation, not a guarantee that external state cannot change
again after the current checkpoint. No new scanner, cache/offline DB lifecycle,
remediation or Stage 10 behavior is included.

### Stage 10A: deterministic Dart formatting quality gate

Stage 10A reuses `Project Rule -> code.verification -> Verification Adapter ->
Rule Evidence v1 -> SYNC binding/finalization`. Formatting is one packaged
adapter, not a new generic checker, Rule schema, Evidence version, quality-gate
result type or configurable command runner. `dart.analyze@1`, `dart.test@1` and
`osv.scan@1` retain their existing semantics.

`dart.format@1` has packaged metadata `executes_project_code=False`,
`uses_semantic_hash=True`, `uses_network=False` and
`global_input_patterns=("**",)`. Empty bounded input `()` is `NOT_APPLICABLE`
without discovery or tool execution. Any non-empty bounded change deliberately
invalidates the complete project (`project_wide_invalidation`); `None` uses
`project_wide`. Trusted discovery with no project-owned Dart files also returns
`NOT_APPLICABLE` without a version probe. There is no selective mapping.

**Discovery authority.** Project System recursively discovers regular `.dart`
files and returns sorted canonical repository-relative paths. It does not
follow symlinks/reparse points and rejects observed unsafe paths or failed
enumeration/inspection/read of non-excluded inputs. At every depth it excludes
case-insensitive infrastructure names `.git`, `.generated`, `.dart_tool`,
`.pub-cache`, `build`, `node_modules`, `.venv`, `venv`, `__pycache__`, `vendor`,
`dist`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.cache`, `.tox`, `.nox`
and `coverage`. Filename suffixes such as `.g.dart`, `.freezed.dart` or
`generated.dart` are **not** exclusions outside these subtrees.

**Fixed read-only execution.** The existing Dart SDK version probe is reused.
Each discovered source is checked separately using:

```text
dart format --output=none --set-exit-if-changed <absolute-explicit-file>
```

The formatter uses `shell=False`, project-root cwd, a 120-second per-process
timeout and a 16 MiB per-stream capture limit through the existing bounded
process runner. The version probe retains its existing 10-second/64 KiB limits.
Paths are positional argv entries, never shell interpolation or project-supplied
flags. There is no `dart format .`, autofix, SDK installation, `pub get`,
`flutter pub get`, `dart fix` or package-manager mutation. Exit semantics, not
unstable human formatter text, are authoritative. The fixed flags and exit
contract are described in the [official Dart formatter documentation](https://dart.dev/tools/dart-format).

**Outcomes.** Per-file exit `0` means formatter-clean and `1` means the formatter
would change that file. All inputs are checked to obtain the complete inventory.
The aggregate is `PASS` when all are clean and `FAIL` when any would change;
aggregate exit code is respectively `0` or `1`. Each unformatted source yields
exactly one sorted concrete finding: canonical relative `path`, null
`line`/`column`, severity `ERROR`, code `dart.format.required`, message
`Dart source is not formatter-clean`. Missing tool, version failure, timeout,
capture overflow, unsupported exit, malformed process result, unsafe input,
observable discovery/content drift or execution failure is
`VerificationAdapterError`/Rule `ERROR`, never vulnerability/formatting `FAIL`.
A later infrastructure error cannot be hidden by an earlier formatting finding.
The existing Rule severity, governed exceptions and SYNC Evidence checks apply.

**Source stability.** F0 before the version/formatter checks binds the complete
sorted source inventory and each file's streamed SHA-256. F1 after all checks
repeats authoritative discovery and hashing; F0 must equal F1. Hashing uses
fixed 1 MiB reads and the existing platform no-follow read primitive, with
regular non-reparse path/handle observations before and after reading.
Addition/removal, content mutation (including same-size mutation with restored
mtime) or observed unsafe transitions are `ERROR`. Handles are closed and no
fallback to an unsafe read is provided. This is observable drift detection, not
an OS snapshot, atomic path resolution or protection against all concurrent
changes. No new binary/config attestation subsystem is introduced.

**Semantic schema v1.** `semantic_sha256` binds exactly `schema_version=1`, the
complete sorted `source_paths`, and sorted `unformatted_paths`. Source bytes,
runtime content hashes, inode/mtime, raw output, absolute paths, PID, timing and
temporary data are excluded. Raw stdout/stderr provenance is incrementally
hashed with length-framed source-path/stream bytes; complete transcripts are
not accumulated across files. Common result hashing still binds `tool_version`.
Equivalent formatter inventory with different raw output retains the same
semantic/result hash and Rule Evidence. Changed source/violating path inventory
changes semantics. Runtime source hashes never enter semantic Rule Evidence.

**Operational limits.** The installed Dart formatter remains authoritative for
its SDK and project configuration. Formatter behavior may depend on toolchain,
language version, formatter settings and package/environment state. Stage 10A
does not fingerprint or manufacture that environment, does not run `pub get`
to create `.dart_tool`, and adds no TTL, cache or environment attestation.
Per-process bounds are not an aggregate project deadline; many source files
cost one formatter process each plus two streaming hash passes. Trusted SDK
behavior is assumed: read-only flags are not an OS sandbox. Human review and
the existing governance remain authoritative for meaning-changing edits.

At Stage 10A acceptance, Stage 10 was **not closed**: 10B/10C remained.
Stage 10A is accepted as the
deterministic Dart formatting quality gate; it does not add strict analyzer policy,
coverage, complexity/duplication metrics, SAST, secrets or later stages.

### Stage 10B: strict Dart static-quality gate

`dart.analyze.strict@1` reuses `Project Rule -> code.verification -> Verification
Adapter -> Rule Evidence v1 -> SYNC binding/finalization`. It is a separate
packaged capability implemented inside the existing Dart analysis module, not
a new checker, Rule schema, Evidence version, quality-severity field or command
runner. Its fixed metadata is `executes_project_code=False`,
`uses_semantic_hash=True`, `uses_network=False`, `global_input_patterns=("**",)`.
Activation belongs to the Dart project's canonical Rule registry; the adapter
does not invent a second project-detection protocol.

**Legacy separation.** `dart.analyze@1` retains its raw-output hashing and fixed
`dart analyze --format=machine --no-plugins .` invocation. INFO diagnostics with
exit 0 remain accepted legacy PASS. Strict analysis does not change legacy
analysis, formatting, tests or OSV contracts.

**Applicability and execution.** Empty bounded evaluation paths `()` return
`NOT_APPLICABLE` without version probing or analyzer execution. Any non-empty
bounded change, including documentation, produces `project_wide_invalidation`;
`None` produces `project_wide`. Both analyze the complete project using only:

```text
dart analyze --format=machine --no-plugins --fatal-infos .
```

Execution uses `shell=False`, project-root cwd, the existing bounded process
runner, a 120-second analysis timeout and 16 MiB per captured stream. The existing
Dart version probe keeps its 10-second/64 KiB bounds. Plugins are disabled. There
is no project-supplied argv, configurable threshold, `minimum_severity` or
`fatal_infos` parameter, SDK installation, `pub get`, `flutter pub get`, `dart fix`
or automatic canonical mutation.

**Strict outcomes.** Exit codes must agree with the normalized diagnostics:

| Normalized diagnostics | Expected exit | Status |
| --- | --- | --- |
| None | 0 | PASS |
| Highest severity INFO | 1 | FAIL |
| Highest severity WARNING | 2 | FAIL |
| Highest severity ERROR | 3 | FAIL |

Every contradiction is infrastructure/integrity `ERROR`, including INFO with
exit 0 and empty findings with nonzero exit. Analyzer-server crash exit 4,
other unsupported/negative/bool exits, malformed output, non-text output, unsafe
or out-of-root diagnostic paths, missing Dart, version failure, timeout, capture
overflow and execution exceptions are controlled `VerificationAdapterError` /
Rule `ERROR`, never diagnostic `FAIL`. Runtime failure detail is sanitized by
the existing consumer boundary.

**Findings and semantics.** The existing machine parser produces sorted,
deduplicated findings with exactly `path`, `line`, `column`, `severity`, `code`,
`message`. Paths are canonical repository-relative paths. Severity remains the
analyzer's INFO/WARNING/ERROR; Rule enforcement severity is independent policy.
`semantic_sha256` hashes canonical UTF-8 JSON (sorted keys, compact separators,
unescaped Unicode) with exactly this semantic schema v1:

```json
{"schema_version":1,"diagnostics":[{"path":"lib/main.dart","line":1,"column":2,"severity":"INFO","code":"LINT","message":"Use a better name."}]}
```

The diagnostics array is the normalized sorted/deduplicated inventory; no
hidden diagnostic type or source-length fields are added. Raw stdout/stderr,
machine-line representation/order, absolute root, timing and PID are excluded.
Raw stdout/stderr SHA-256 remain validated execution provenance at the adapter
result boundary, but do not enter stable result hashing or Rule Evidence.
Common result hashing still binds adapter/tool identity and version, evaluation
mode, status, exit code and normalized findings. Equivalent findings with
different raw ordering or stderr therefore retain identical semantic/result
hashes and Evidence. A change to any finding field changes those hashes.

**Governance and fresh SYNC checks.** Existing Rule severity, exception policy
and governed waiver semantics apply unchanged. Infrastructure ERROR remains
blocking and cannot be waived. For a new finalization, the existing fresh
`sync_verify` recheck executes strict analysis again before staging/commit.
Semantic-equivalent PASS with transcript volatility can reach dry-run `prepared`;
a fresh valid INFO/exit-1 FAIL changes Evidence and blocks stale finalization
even with nonblocking WARNING Rule enforcement and unchanged Git/files/fingerprint.
Previously proven committed retries retain the existing finalization protocol.

**Limits.** Installed Dart, analyzer configuration, language/package resolution
and prepared `.dart_tool` environment remain external prerequisites. There is
no dependency selection map, TTL/cache, configuration/binary attestation or OS
sandbox. Fixed flags and capability metadata are not an enforced network or
filesystem sandbox; trusted SDK behavior is assumed. No live Dart/network is
required by the regression tests. Human governance remains authoritative for
meaning-changing edits.

At Stage 10B acceptance, Stage 10 was **NOT closed**: **10C remained**.
Stage 10B adds no coverage, mutation
testing, complexity/duplication metrics, method-length limit, SAST, secrets,
SBOM/licenses, plugins, autofix, arbitrary commands, Stage 11 or Stage 12.

### Stage 10C: Code Quality Gates closure

Stage 10 consists of three bounded steps:

- **10A** — `dart.format@1`, the deterministic formatting gate.
- **10B** — `dart.analyze.strict@1`, the strict static-quality gate.
- **10C** — composition/integration closure through the existing Rules,
  Evidence v1, validation and SYNC lifecycle.

The combined fixture activates independent `QUALITY-001` (`dart.format`) and
`QUALITY-002` (`dart.analyze.strict`) Rules at `project_validate` and `sync_verify`.
Only the Dart process transport is replaced in tests: parsing, discovery, source
stability, Rule evaluation, Evidence construction, validation, planning,
verification and dry-run finalization use the existing implementations.

The composition checks establish:

- Both adapters execute and produce separately identified Rule results. Combined
  Evidence remains schema v1 and is identical for opposite registry insertion
  orders with otherwise identical inputs.
- Multiple valid failures do not short-circuit evaluation. Each Rule retains its
  own findings, analyzer/formatter status and result hash. Gate consequences
  derive from that Rule's enforcement severity, not diagnostic severity or a new
  quality-specific policy.
- Semantic Evidence contains each adapter's `semantic_sha256`, not raw
  stdout/stderr hashes. Cross-Rule result-hash substitution and swapped adapter
  identities are rejected by the existing contracts.
- Real `plan_sync -> verify_sync -> finalize_sync` executes both adapters freshly
  at the final `sync_verify` recheck. Equivalent PASS with raw transcript
  volatility preserves both semantic/result hashes and the exact Evidence
  binding, allowing dry-run `prepared` without requesting commit or push.
- Format-only, strict-only and combined semantic drift change the affected
  Evidence while preserving the unaffected Rule result. Stale finalization is
  rejected even with nonblocking Rule severities and unchanged file bytes,
  HEAD, index, Git diffs and working-tree verification fingerprint.
- Either adapter's infrastructure ERROR remains ERROR, cannot become FAIL or
  WAIVED, and blocks finalization independently of the other adapter's valid
  PASS/FAIL. Secret transport exception text is absent from captured output,
  Evidence, validation issues and finalization reports.

No production adapter, checker, schema, Evidence version, exception behavior,
SYNC finalization protocol, package layout or version is changed by 10C.

**Closure statement for independent main-chat acceptance:**
**Stage 10 — COMPLETE**. This statement becomes authoritative only after the
independent integrity review; the implementation candidate does not self-approve
closure or declare readiness to commit.

The closure is limited to deterministic Dart formatting and strict analyzer
quality gates through existing Rule Evidence/SYNC governance. It does not mean
all code-quality or security concerns are implemented. Coverage, mutation
testing, regression strength, complexity/duplication thresholds, SAST, secrets,
SBOM/licenses, plugins, autofix and arbitrary commands remain out of scope.
Installed toolchain/configuration/environment assumptions, lack of OS sandbox
and incomplete environment attestation remain as documented in 10A/10B. These
tests require neither live Dart nor network and do not attest those live systems.

**Next: Stage 11 — Mutation and Regression Quality.** No Stage 11 behavior is
implemented or implied by this closure.

### Stage 11A: strict Dart mutation-quality foundation

`dart.mutation.strict@1` reuses `Project Rule -> code.verification ->
Verification Adapter -> Rule Evidence v1`. Its registry metadata is
`executes_project_code=True`, `uses_semantic_hash=True`, `uses_network=False`,
`global_input_patterns=("**",)`. Empty bounded paths are NOT_APPLICABLE without
any process invocation; every non-empty bounded set becomes
`project_wide_invalidation`; unbounded evaluation is `project_wide`.
There is no selective mutation mapping or new Rule/Evidence/checker schema.
Existing adapters' contracts are unchanged.

**Pinned compatibility contract:** only `dart_mutant 0.1.0`, upstream release
commit `231c599c14e3a1bcafeb408a042f98cad15ea1f9`, is supported. The fixed
`dart_mutant --version` probe must exit zero and emit exactly
`dart_mutant 0.1.0` (with optional terminal newline). Missing executable,
malformed output, nonzero probe or any other version is ERROR. The probe
attests the version string, not a binary digest or upstream commit identity.
The adapter never installs/downloads a tool or makes network requests.

The pinned release's declared `lib/**/*.dart` CLI glob is **not authoritative**:
its discovery does not consume `args.glob`. With `--path .`, the actual
engine-visible domain is all applicable regular Dart sources in the shadow
tree, not lib-only. `bin/`, `tool/`, `example/`, root sources and `.dart_tool/`
can be eligible where present. No production-code meaning is inferred from
these directories. No `--glob` is passed and it is not claimed to restrict
scope. Independent discovery freezes the pinned engine exclusions:

```text
**/*.g.dart
**/*.freezed.dart
**/*.mocks.dart
**/generated/**
**/test/**
**/*_test.dart
```

These match case-sensitively against engine-style `./` relative paths, with
wildcards spanning separators as in the pinned Rust glob defaults. Tests are
retained for execution but excluded from mutation targets by these defaults.
Adapter-owned shadow exclusions, at any directory depth, are `.git`,
`.generated`, `build`, `node_modules`, `__pycache__`, `.pytest_cache`,
`.mypy_cache`, `.ruff_cache`, `.cache`, `.tox`, `.nox`, `coverage` and
`mutation-reports` (case-insensitive directory-name comparison). No additional
product-code exclusions restore a lib-only model. Ordinary project files,
fixtures and `.dart_tool` are retained. No independently discovered eligible
source means NOT_APPLICABLE with no version, baseline or mutation invocation.

**Shadow and green-baseline precondition:** canonical files are never
intentionally used as mutation/baseline cwd or written back. A disposable
temporary root outside the canonical tree contains separate `project/` and
`report/` directories. Copying uses regular-file no-follow reads, bounded
1 MiB chunks, byte digests and file modes, rejects symlinks/reparse/special
entries in retained subtrees, and checks ancestry/observable inventory and
content drift. It does not create hardlinks, touch Git metadata or use Git
worktree/stash/reset. Success and errors clean the temporary root.

Copy consistency is observable, **not** a globally atomic OS snapshot;
concurrent edits cannot be made globally atomic. The pinned engine does not
establish its own green baseline. After the version probe and trusted copy,
the adapter runs exactly `dart test --reporter=compact` in the shadow.
Only exit zero establishes the prerequisite. Red baseline, missing Dart,
timeout, capture overflow, execution exception or malformed process result
is ERROR, never mutation FAIL, and prevents mutation execution. Baseline
changes to the eligible source inventory/bytes/modes also fail closed.
An independent `dart.test` Rule does not replace this prerequisite.

The mutation argv is fixed, in this order:

```text
dart_mutant --path . --parallel 1 --timeout 300 --threshold 0 --quiet --json --ai none --output <temporary-root>/report
```

Sequential execution is mandatory. Threshold zero neutralizes the external
tool's score gate: Project System owns the verdict through mutant statuses.
No sampling, incremental/base-ref, coverage input, operator selection, AI,
custom test command, project glob/excludes/threshold or arbitrary flags are
accepted. All processes use `shell=False`, UTF-8 and bounded stdout/stderr
capture of 16 MiB **per stream**. Version, baseline and whole mutation-process
timeouts are respectively 30, 300 and 7200 seconds; the engine's per-mutant
timeout is 300 seconds, using the same fixed test-suite budget as the baseline
and existing `dart.test` adapter, not a new project policy. This avoids accepting
mutant Timeouts merely because the green unmutated suite needs more than a
shorter mutant budget. Mutation exit must be zero; **exit 1 is ERROR**, not a
quality failure. Signals, missing tools, malformed objects or transport
failures are sanitized at the existing Adapter error boundary.

**Machine protocol:** only no-follow regular `report/mutation-report.json`
(maximum 64 MiB) is parsed. Require the pinned `schemaVersion: "1"`, `files`
mapping, `language: "dart"`, mutant arrays and well-formed positive integer
start/end positions in non-reversed order. Native relative `./` / `.\\` and
Windows separators normalize to repository-relative POSIX paths; absolute,
traversal, noncanonical, excluded and non-target paths fail closed. Every
reported file must be an independently discovered eligible source. Duplicate
JSON keys, path aliases, duplicate mutant identities (including contradictory
statuses), non-finite numbers, malformed/missing report or unsupported
structure/language/status are ERROR. Target sources with an empty inventory
are ERROR, not implicit PASS. Console prose never fabricates evidence.

The pinned JSON statuses are `Killed`, `Timeout`, `Survived`, `NoCoverage`,
`CompileError`. At least one mutant, all Killed/Timeout, means PASS.
Survived/NoCoverage with no infrastructure status means FAIL. CompileError,
and unsupported RuntimeError/Pending/Ignored/unknown values, mean ERROR,
even if other mutants survive. Do not claim the pinned release emits every
Stryker status. Tool IDs, descriptions, scores, thresholds and projectRoot
are not semantic authority.

**Semantic schema v1** hashes canonical UTF-8 JSON (sorted keys, compact
separators, no non-finite values):

```json
{"schema_version":1,"mutants":[{"path":"lib/example.dart","start_line":1,"start_column":1,"end_line":1,"end_column":2,"mutator_name":"Arithmetic","replacement":"-","status":"Killed"}]}
```

Each record contains exactly those eight fields; the complete inventory sorts
by them in the listed order. No target source inventory, root, temporary path,
ID, description, time/duration/PID, score, threshold, HTML or transcript enters
the semantic digest. Raw framed version/baseline/engine stdout/stderr digests
remain Adapter-boundary provenance but are absent from semantic result hashes
and Rule Evidence. Equivalent ordering/logs/temporary roots/IDs/descriptions
preserve hashes and Evidence; any mutant identity/status change changes them.

Survived/NoCoverage findings use ERROR severity, canonical path/start position,
codes `dart.mutation.survived` / `dart.mutation.no_coverage`, and stable messages
`Mutation survived the Dart test suite (<mutator_name>)` /
`Mutation has no test coverage (<mutator_name>)`, using only the normalized
semantic mutator name as the distinguishing message value. Each undetected
mutant emits its own finding; different mutator names at the same start
position/status remain distinct. No replacement, original source, tool ID,
temporary path or raw transport text is exposed. Findings are sorted, not
silently deduplicated by the producer. If separate identities remain
indistinguishable under these permitted finding fields, the unchanged generic
boundary rejects the result as ERROR rather than losing a mutant. Existing Rule severity
controls blocking/nonblocking FAIL and existing exception governance applies.
Infrastructure ERROR is never WAIVED. No numeric score-policy language exists.

**Limits:** Dart-only, no Flutter claim or auto-switch to `flutter test`.
The shadow is not an OS sandbox: project tests can perform external side
effects/network access, absolute package references can escape the shadow,
and installed tools/environment are not fully attested. Retaining
`.dart_tool` does not guarantee its package configuration is relocatable.
The adapter does not request network access, but cannot enforce offline test
behavior. **Classification-validity trust boundary:** `Killed` is
engine-reported under pinned v0.1.0. Its runner maps `dart test` exit zero to
Survived and nonzero to Killed; thus a mutant whose test process exits nonzero
because of a compile/type failure may be classified as Killed rather than a
test-detected behavioral failure. A separately emitted `CompileError` remains
ERROR. Stage 11A establishes **provisional engine-reported mutation evidence**,
not full mutant-validity attestation. **Stage 11B MUST resolve or contain this
classification-validity gap before Stage 11 can close and before mutation
evidence is treated as final authoritative regression-strength proof.**
Stage 11A does not parse human Dart-test stderr, vendor/patch the tool or build
a replacement mutation runner. Mocked tests attest the adapter
contract, not live toolchain correctness. No Flutter, coverage-percentage
gate, sampling, cache, nightly scheduling, AI mutations, SAST, secrets,
complexity/duplication, Stage 12/13 or upstream modification is included.

**Stage 11 is NOT closed. 11B/11C remain.** This implementation candidate does
not self-approve acceptance, mutation/regression quality closure or readiness
to commit; independent main-chat integrity review is required.

### Stage 11B: independent mutant validity and behavioral attestation

`dart.mutation.strict@2` supersedes the provisional Stage 11A `@1` contract.
The Stage 11A section above records the historical foundation, not a second
selectable adapter. The Rule still selects `adapter: dart.mutation.strict`;
the registry and result now bind version `2`. Rules/Evidence v1, common result
validation, severity and exception governance are unchanged. Engine-reported
v1 evidence is not interchangeable with independently attested v2 evidence.

The engine remains pinned to `dart_mutant 0.1.0`, release commit
`231c599c14e3a1bcafeb408a042f98cad15ea1f9`. Its `Mutation::new` encodes
`start_line` as a 1-based tree-sitter row and `start_column` as a 1-based
**UTF-8 byte column**. `end_line` must equal `start_line`, including multiline
originals; `end_column - start_column` is the positive UTF-8 byte length of
the original, **not** a visual same-line endpoint. LF bytes determine row
starts; CRLF and non-ASCII bytes are preserved. The start must lie in the
encoded row, the span must fit the baseline buffer, and its endpoints must
be UTF-8 boundaries. The complete source must be valid UTF-8. Reconstruction
is exactly `before + replacement.encode('utf-8') + after`. Descriptions are
never reconstruction authority.

Every mutant, including Survived/NoCoverage, requires a JSON `id` of 32
lowercase hex characters matching the pinned engine's MD5 of UTF-8
`<raw JSON file key>:<start_line>:<decoded original>:<replacement>`.
Raw path spelling (including native separators and `./`) is used for this
compatibility check; canonical path normalization remains the semantic
identity. MD5 is engine compatibility, not a cryptographic security claim.
Impossible spans, malformed IDs, source contradictions, duplicate identities,
unsupported statuses or CompileError are ERROR. No original, ID, MD5 or byte
offset is emitted into Evidence.

Execution order is: version probe; trusted disposable engine-shadow copy;
baseline static validity; green compact baseline (300 seconds); engine;
independent engine-restoration proof; strict report parsing/reconstruction;
independent replay of each Killed/Timeout in normalized mutant order. Baseline
analysis precedes project-code execution so it attests the fresh
canonical-derived snapshot, not a shadow potentially modified by tests through
retained non-target configuration, test or package inputs. Existing target
source-integrity checks run after analysis and after the compact baseline;
this does not introduce a blanket non-target filesystem immutability policy. Before
report trust, the engine-shadow applicable source inventory, modes and bytes
must equal the initial trusted sources. Leftover mutations are ERROR, never
repaired into canonical files.

Static validity uses fixed `dart analyze --format=machine --no-plugins .`
with 120 seconds and 16 MiB capture per stream. The existing qualified
`dart.analyze` machine parser and severity/exit semantics are reused privately,
without modifying old adapters. ERROR-free unmutated baseline is mandatory.
Warnings/INFO are allowed, including their corresponding exit codes; malformed
protocol, unexpected/contradictory exit, timeout or infrastructure failure is
ERROR. Human analyzer prose is never verdict authority. Diagnostic/suite paths
are checked for contained regular no-follow ancestry before the reused parsers
resolve them.

Each Killed/Timeout gets a **fresh separate replay shadow copied from unchanged
canonical state**, not from the engine workspace or a previous replay. The
canonical complete applicable inventory/bytes are checked against the initial
snapshot before/after each copy and after replay. No hardlinks, Git worktrees,
stash/reset or generic sandbox are used. The target must match trusted bytes
and mode before an owned no-follow handle write; exact mutated bytes and all
other applicable source bytes/modes/inventory are verified before and after
replay commands. Symlink/reparse/special ancestry fails closed. Disposable
workspaces are cleaned on success and failure; no canonical repair is attempted.

The mutant must also pass ERROR-free static validity before fixed
`dart test --reporter=json` (300 seconds, 16 MiB per stream, `shell=False`).
The existing qualified `dart.test` JSON reporter parser and structured
exit/verdict consistency are reused privately. Killed requires structured FAIL;
assertion failures and structured runtime errors count after static validity.
PASS, NOT_APPLICABLE, timeout, malformed protocol or infrastructure failure
contradict Killed and produce ERROR. Timeout requires a second independent
timeout at the same 300-second budget; structured FAIL, PASS or any different
process/protocol outcome is ERROR. No Killed/Timeout status conversion occurs.

Survived/NoCoverage remain conservative FAIL with the stable Stage 11A findings;
they require reconstruction/ID checks but not behavioral replay. **ERROR
dominates FAIL**, even in mixed reports. PASS requires at least one mutant,
valid reconstruction of every mutant, independent attestation of every positive
status, and no Survived/NoCoverage. Bounded-empty/no-target applicability still
returns NOT_APPLICABLE without tool execution.

The stable semantic payload is exactly:

```json
{
  "schema_version": 2,
  "attestation": "independent_replay_v1",
  "mutants": [
    {
      "path": "lib/main.dart",
      "start_line": 1,
      "start_column": 28,
      "end_line": 1,
      "end_column": 29,
      "mutator_name": "Arithmetic",
      "replacement": "-",
      "status": "Killed"
    }
  ]
}
```

The same eight normalized mutant fields are sorted canonically. There are no
extra per-mutant semantic fields. Analyzer/replay output, originals, engine
IDs/descriptions, raw path spelling, offsets, scores, durations, PIDs and
temporary paths are excluded. Bounded version/baseline/engine/analyzer/replay
transport contributes only to raw provenance hashes, not semantic/result hashes
or Rule Evidence. Findings and controlled errors never expose source originals,
replacement text, transcripts, exception secrets or temporary paths.

Existing `verify_sync` binds version-2 Rule Evidence and `finalize_sync` freshly
executes this adapter through the existing lifecycle. Equivalent report order,
temporary paths and allowed raw analyzer/replay transport preserve exact binding
and dry-run prepared state. Fresh Survived/NoCoverage changes Evidence; fresh
replay PASS or mutant analyzer ERROR produces Rule ERROR. All block stale
finalization without changing canonical/Git identity. There is no second
mutation-specific SYNC mechanism.

This is intentionally expensive: one full copy and analyzer/test pair per
positive mutant, plus baseline analysis and the engine. Sequential isolated
copies avoid cross-mutant state contamination, but are not an OS sandbox or
atomic filesystem snapshot. Project code/tools can have external effects;
absolute package references, non-relocatable `.dart_tool`, installed environment,
flaky tests and nondeterministic timeout transitions remain limitations. Such
observable replay contradictions fail closed, not silently downgraded. Mocked
qualification does not attest a live Dart toolchain. No sampling/cache,
thresholds, Flutter mutation, generator or upstream changes are introduced.

**Stage 11 is NOT closed. 11C remains.** Stage 11B does not self-approve
acceptance, commit readiness or final regression-strength closure.

### Stage 11C: Regression-quality composition and closure

The regression-quality stack composes four independent active deterministic
Rules using the existing `code.verification` checker at `project_validate` and
`sync_verify`:

```text
dart.format@1
  -> dart.analyze.strict@1
  -> dart.test@1
  -> dart.mutation.strict@2
```

This is a capability map, not a dependency graph or a mega-check. Formatting
evidences canonical source formatting; strict analysis evidences static-quality
validity; direct tests evidence independently normalized regression-suite
inventory/outcomes; mutation evidences test sensitivity for the adapter's
eligible mutation domain. Skills orchestrate; Rules enforce. The adapter
implementations remain separate, with no new inter-adapter dependency.

Direct `dart.test` PASS does **not** imply mutation PASS: green tests may coexist
with Survived/NoCoverage mutation FAIL. Conversely, mutation PASS does not replace
the independently identified `dart.test` Rule Evidence. The mutation adapter's
own green compact baseline is an internal trust precondition, not evidence for
the separate direct-test Rule. A red direct suite is test FAIL; a red mutation
internal baseline is mutation ERROR, never a mutation-quality FAIL. Neither
claim is inferred from the other Rule.

Each Rule preserves its identity, adapter/version, semantic/result hashes,
findings and status. Stable Rule-ID evaluation and canonical registry hashing
make registry insertion order irrelevant. Failures do not short-circuit other
Rules. Raw stdout/stderr, report ordering, replay transcripts, disposable
workspace names and non-semantic reporter timing are not semantic Rule Evidence.
Isolated format, strict-analysis, direct-test inventory/outcome or mutation
outcome changes affect only the corresponding Rule's result Evidence; the
combined Evidence fingerprint consequently changes. Cross-Rule result/hash
substitution and cross-adapter identity/version substitution are rejected at
the existing generic trust boundaries.

Evidence remains **Rule Evidence v1**. No Rule, Evidence or checker schema changes
are introduced by Stage 11. Enforcement severity and exception governance remain
generic: valid FAIL blocks for ERROR/BLOCKING severity, not WARNING/INFO;
infrastructure ERROR always blocks, dominates quality FAIL and cannot become
WAIVED. There is no regression-specific exception policy or combined checker.

The real existing lifecycle supplies the closure gate:

```text
plan_sync -> verify_sync -> finalize_sync (fresh sync_verify recheck)
```

Verification binds all four independently identified results. Finalization
re-executes all four Rules, even when one fails. Semantically equivalent fresh
results preserve the exact Evidence binding and reach dry-run `prepared` without
commit/push. Selective semantic drift in any layer rejects stale finalization,
even with unchanged Git/files/verification fingerprint. An independent mutant
replay contradiction or infrastructure failure produces ERROR and blocks
finalization while leaving the other three results intact. No mutation-specific
SYNC or new freshness mechanism is introduced.

**Stage 11 — COMPLETE** is conditional on independent main-chat integrity review
of this Stage 11C candidate. Until that review accepts it, this is a composition
and closure candidate, not self-approved closure, release or commit readiness.
The 11A/11B statements above describe their historical stage boundaries.

Upon acceptance, COMPLETE means deterministic regression-suite Evidence and
strict mutation-quality Evidence exist; positive mutations are independently
reconstructed and statically/behaviorally attested; those claims compose with
existing format/analyze gates; and fresh SYNC finalization detects semantic drift
or integrity contradiction. It does **not** mean line/branch coverage, exhaustive
mutation generation, Flutter mutation, solved flaky tests, fully attested tool
binaries/environment, an OS sandbox, performance benchmarks, or SAST/secrets/
SBOM/licenses. The 11A/11B eligible-domain and toolchain limits remain: sequential
fresh copies are not atomic filesystem snapshots; project code/tools may have
external effects; package relocation, installed environment, flaky tests and
timeout transitions can still fail closed. Mocked transports qualify composition,
not a live Dart toolchain.

No coverage/mutation percentage, minimum test count, test/source ratio,
performance threshold, retries, arbitrary commands, changed-lines-only selection,
sampling, cache or nightly quality policy is added.

**Next: Stage 12 — Task Specification, Risk, and Task Obligations.** Stage 12 is
not implemented here.

### Stage 12A: Task Specification v1 foundation

`project task <target>` retains its CLI grammar, printed context directory and
`(output, manifest)` return contract. In addition to `context.md` and
`manifest.json`, it writes `task-spec.json` in that existing generated directory.
This is a **derived execution contract**, never a canonical Task object or
another source of project truth. The strict packaged `task-spec.schema.json`
rejects missing/unknown fields and malformed nested structure. A narrow loader
also rejects duplicate JSON keys, oversized input, unsafe paths and inconsistent
identity bindings.

The exact fields are:

- `schema_version`: `1`;
- `profile`: `project-system-task-spec-v1`;
- `project_id`: the exact canonical `project.yaml` `project.id`;
- `base_commit`: current Git HEAD, exactly 40 lowercase hex characters;
- `target`: `{id, type, path, sha256}`, from the canonical object record selected
  by internal ID, with a repository-relative POSIX path (including any slug) and
  SHA-256 of exact current file bytes;
- `mode`: the requested mode after strict non-empty whitespace trimming, with
  no new mode taxonomy;
- `verification_checkpoint`: `task_verify` (a binding, not an evaluation);
- `write_scope`: `{canonical, effective}`, copied from the already-computed task
  manifest, sorted and deduplicated; only canonical repository-relative paths or
  anchored patterns, with no traversal, absolute paths, `.git` authority or
  generated-context output authority;
- `skills`: `{registry_sha256, selected}`, reusing existing manifest evidence;
  selected entries `{name, path, sha256}` are sorted by name and bind
  `.agents/skills/<name>/SKILL.md` without copying Skill contents.

Canonical write scope is the task authority ceiling; effective scope is a
capability-reduced subset and may never widen that ceiling. Every effective
pattern must be fully contained by at least one canonical pattern, using the
existing Skills write-scope grammar and containment implementation: exact
repository-relative paths or normalized tree patterns ending in `/**`, not
Rule-style `*`, `?` or character-class globs. Exact authority contains only the
same exact path; a tree contains its base and paths/subtrees below that base at
a `/` boundary. Empty effective scope is valid. This relationship is validated
intrinsically for persisted specs, including builder/loader paths; selected
Skill names alone do not establish or expand authority.

Git identity uses a bounded `shell=False` invocation of
`git rev-parse --verify HEAD^{commit}` with a 30-second timeout and bounded
capture. Missing Git, nonzero exit, malformed/ambiguous HEAD, an invalid target
record, unsafe target path or unavailable valid Skill evidence fails closed:
no placeholder commit, registry digest or invented identity. Task preparation
therefore requires an established Git HEAD and Skills evidence; direct context
generation, existing bootstrap and legacy `project sync <OBJECT-ID>` retain
their previous behavior and do not acquire this new task-spec precondition.

Serialization is UTF-8 JSON with stable sorted keys, two-space indentation and
exactly one terminal LF. There is no self-hash (`task_spec_sha256`). Git base,
target identity/type/path/bytes, mode, canonical/effective scopes and selected
Skill/registry evidence affect these bytes. Context budget alone does not.
There are no absolute checkout/output paths, context content/hash, manifest
path, timestamps, PID, hostname, username or random run identity in the spec.
Equivalent semantic inputs in another checkout yield identical bytes.

A dirty tree is allowed: Git base and exact current target bytes are separate
bindings. This is **not** a full working-tree snapshot, atomic filesystem
snapshot, freshness/invalidation proof, implementation verification or approval.
Sequential reads and context generation retain their existing limits; a failed
task-spec write may leave disposable context/manifest output. Project code is
not executed by this foundation; no sandbox or new trust/approval mechanism is
introduced.

Stage 12A does not aggregate acceptance criteria, calculate/resolve risks or
mitigations, generate task obligations, evaluate `task_verify` Rules or produce
their Evidence, compare implementation changes, or complete/finalize tasks.
**Stage 12 is NOT closed: 12B and 12C remain.** This foundation is not
self-approved Stage 12A acceptance and makes no Stage 13 claim.

### Stage 12B: Requirements, Risks, and Task Obligations

`project task <target>` additionally writes `task-obligations.json` beside
`context.md`, `manifest.json`, and the unchanged Task Specification v1 artifact.
This is derived execution data, never a second canonical source of truth.
Bootstrap, direct context, and legacy `task(..., sync=True)` retain their existing
behavior and gain neither this artifact nor its creation preconditions.

Requirements are selected only from canonical structured metadata: a requirement
target includes itself; a feature includes exactly its declared `requirements`
references; all other targets select none. References must exist, be requirements,
and be unique. There is no prose, naming, domain, graph-proximity, AI, or recursive
`depends_on` inference. Selected sources are schema-valid, safely contained objects
whose internal identity agrees with the canonical filename/type/directory.

Each source binds its exact canonical file bytes with SHA-256. Requirement records
contain ID, relative path, digest, lifecycle status, nullable priority, and exact
ordered acceptance criteria (missing means an empty list). Only **active** criteria
produce obligations: `<REQ-ID>#acceptance-<four-digit one-based index>` with exact
text, source ID, index, and `acceptance_criterion` kind. Repeated text at distinct
indices remains distinct. Non-active sources remain bound but create no obligations.
The four-digit locator supports at most 9,999 ACTIVE criteria per requirement.
The shared builder/validator/loader generation boundary checks every active source
before expanding any obligation records; overflow fails closed without truncation
or changing the profile. Non-active criteria create no locators and remain governed
by the artifact byte limit, not the active locator limit.

Risks are selected only when structured `affects` intersects the target ID or a
selected requirement ID, or when the target itself is that risk. All risk lifecycle
states remain included. Each record binds status, nullable severity/mitigation,
and a sorted unique identity set of `affects`; duplicate source mentions are set
membership, not new obligations, and canonical data is not rewritten. Missing
optional severity or mitigation is null, not an invented default. Risk mitigation
is not an automatic obligation; no severity threshold, PASS/FAIL, or risk enforcement
policy is introduced. An unreadable risk inventory or malformed structured `affects`
fails closed because deterministic relevance cannot be established.

The builder validates the persisted `task-spec.json` through the Stage 12A trusted
loader and binds SHA-256 of its **actual persisted bytes**, not reserialized JSON.
Target ID/type/path/hash contradictions against the current canonical source fail
closed; the spec is never silently regenerated to hide drift. This is creation-time
binding, not an atomic multi-file filesystem snapshot or a later freshness lifecycle.

The packaged strict `task-obligations.schema.json` rejects unknown fields. Its exact
top-level fields are `schema_version` (1), `profile`
(`project-system-task-obligations-v1`), `task_spec_sha256`, `requirements`, `risks`,
and `obligations`. The bounded UTF-8 loader rejects duplicate JSON keys and non-finite
values. Semantic validation enforces source identity/path consistency, sorted unique
inventories and affects, exact obligation IDs/indices/text, and complete one-to-one
coverage of active criteria without orphan, duplicate, missing, or inactive obligations.

Serialization uses sorted keys, indent 2, `allow_nan=False`, and exactly one terminal
LF. Identical selected source bytes and spec bytes produce identical artifact bytes
across context budgets and checkout locations. No timestamp, generated path, budget,
context hash, environment identity, random ID, or self-hash is included. Changes to
unselected/unrelated valid sources alone do not change this artifact.

**Stage 12 is NOT closed. 12C remains.** Stage 12B does not execute `task_verify`,
Rules or completion Evidence, compare implementation diffs with task write scopes,
map criteria to tests/checkers, assign outcomes, finalize tasks, or grant human
approval. This implementation candidate is not self-approved Stage 12B acceptance.

### Stage 12C: Task Verification lifecycle

This is a **Stage 12C candidate**, not independent acceptance or Stage 12 closure.
The published Task Specification v1 and Task Obligations v1 byte contracts remain
unchanged. `project task <TARGET> [--budget small|medium|large]` now also creates
`task-baseline.json` beside its existing context, manifest, spec and obligations.
`project task verify <TARGET> --budget small|medium|large` consumes that existing
directory without creating or regenerating missing/stale contracts. Creation-only
`--mode` and `--skill` are rejected for verification, even an explicit default mode.
Bootstrap, direct context, legacy SYNC, project validation and SYNC finalization
keep their existing behavior. There is no task finalization or lifecycle reset command.

#### Dirty task-start baseline

Tasks may start with dirty tracked files and unrelated untracked owner work.
Therefore `git diff base_commit` alone is **not** a task delta. Before generating
context/artifacts, task creation collects Git-visible paths differing from HEAD
(effective tracked modifications, staged additions/modifications/deletions and
Git-reported untracked files), with renames represented as delete + add. Each path
binds its effective filesystem state, not index contents. Baseline entries are
sorted/unique canonical POSIX repository-relative paths:

```json
{"path": "docs/example.md", "state": "file", "sha256": "<64 lowercase hex>", "bytes": 123}
```

An absent path instead has `state: "absent"`, `sha256: null`, `bytes: null`.
The strict packaged `task-baseline.schema.json` has exactly `schema_version: 1`,
`profile: "project-system-task-baseline-v1"`, `base_commit`, `entries`,
`task_spec_sha256` and `task_obligations_sha256`. The last two bind **actual persisted
bytes after writing** spec/obligations. No clock, user/host, randomness or absolute
path participates. Input file hashing streams bounded chunks. Git executes fixed
arguments with `shell=False`, a 30-second timeout and bounded output capture.
Git must identify this project as the repository root. Unsafe, non-UTF-8,
symlink/reparse paths and non-regular inputs fail closed.

`.git/**` and `.generated/**` are excluded. Arbitrary ignored build trees are not
enumerated; ignored files outside Git's reported set are outside this contract.
Already tracked ignored files remain Git-visible. Both HEAD-to-worktree and
HEAD-to-index path inventories are included, so staged changes cannot disappear
when filesystem bytes have returned to HEAD. This binds filesystem bytes/size,
not index contents or a generic full-filesystem/file-mode immutability policy.
Changes in Git-visible inventory membership still follow the delta-map rule below.

Identical task generation in the same TARGET + budget directory is idempotent.
A different baseline, spec or obligations for that directory is rejected rather
than silently blessing edits as a new start. Failed creation may leave disposable
context/manifest or partially created spec/obligations, but cannot reset an existing
baseline or its bound contracts. Deliberate lifecycle reset is not designed here.

#### Binding, freshness and scope

Trusted bounded loaders reject malformed/duplicate-key/non-finite/invalid-UTF-8/
oversized JSON, unknown fields and inconsistent state/path/hash combinations.
Verification binds exact spec/obligations/baseline bytes; the obligations spec hash
and both baseline artifact hashes must agree. Project ID, selected target,
`task_verify` checkpoint and current HEAD must agree with the persisted task base.
There is no automatic rebasing/rebinding when HEAD moves.

Task definition freshness rebuilds Stage 12B obligations **in memory** from the
persisted validated spec and current canonical layer, using the existing builder
and serializer. Exact byte equality is required. Target bytes, explicit feature
requirement selection, selected requirement criteria/status/priority and relevant
risk bytes/status/severity/mitigation/affects drift invalidate the contract.
Unselected/unrelated valid sources do not become inferred obligations. The exact
Skills registry bytes and every selected safe Skill path/byte digest must still
match the spec; a Skill name alone never establishes authority.

Current Git-visible state uses the same representation as baseline. Task delta is
the sorted union of keys where baseline and current entries differ. Unchanged
pre-existing dirty/untracked owner files are not task changes; further edits,
reversions and disappearance are changes. Renames expose both old/new paths.
Every delta path must fit `task_spec.write_scope.effective`, using existing Skills
exact/tree (`/**`) containment. Canonical scope is only the ceiling, not executor
authority. No prefix-neighbor escapes or automatic scope expansion are permitted.
An empty effective scope permits only zero changes. Derived output stays excluded.

#### Common validation and reports

Only after binding/freshness/scope pass does verification call the existing pipeline:

```python
validate_report(root, rule_checkpoint="task_verify",
                rule_base_commit=spec["base_commit"],
                rule_evaluation_paths=tuple(task_changed_paths))
```

The evaluation paths are the **actual task delta**, never all project files,
the full write ceiling or all pre-existing dirty files. Common complete-evaluation,
governed exception and severity semantics remain authoritative. Common Rule FAIL
follows its enforcement severity; checker ERROR remains ERROR. BLOCKING/ERROR
validation fails verification. No new risk thresholds or task checker engine exist.
Returned Rule Evidence v1 is serialized by `rule_evidence_to_dict()` and retains
its existing fingerprint. No active Rules permits `rule_evidence: null`.

Verification also rechecks bound inputs, HEAD and task state after common validation;
project-code side effects cannot seal a changed snapshot as the verified state.
This is **not an OS sandbox**, atomic filesystem snapshot or protection against
a concurrent hostile process. Report hashes are integrity bindings, not signatures
or human approval; coordinated rewriting/re-hashing is not authentication.

`task-verification.json` uses the strict packaged
`project-system-task-verification-v1` profile. It binds spec/obligations/baseline
SHA-256, base/current HEAD, target/checkpoint, sorted delta/effective scope/outside
scope, exact current `task_state` entries for delta paths, validation issues/counts,
common Rule Evidence, obligation/risk counts and deterministic result. For a reverted
dirty path no longer in the dirty inventory, `task_state` still snapshots its actual
current bytes; a deleted untracked file is represented as absent.

`working_tree_fingerprint` is common canonical semantic SHA-256 of base commit,
the three artifact hashes, effective scope, delta paths and current delta entries.
Equivalent relocated state produces the same fingerprint. Report integrity is
canonical SHA-256 of the report excluding `verification_integrity`; this does not
replace or fork the nested common Rule Evidence fingerprint. Strict report loading
checks schema, scope/result/count relationships, lifecycle identities, fingerprints
and the common Evidence projection. JSON uses UTF-8, sorted keys, indent 2,
`allow_nan=False` and one terminal LF. New lifecycle/report writes are atomic and
guarded for parent containment/symlink/reparse paths, only under `.generated/**`.

`task-verification.md` is a human projection, not trusted lifecycle input. Bound
scope failures persist `SCOPE_FAIL` without running Rules; validation failures
persist `VALIDATION_FAIL`. Successful verification persists `PASS`. Untrusted/
missing/stale input failures do not create a new trusted report (any previous
derived report is historical, not automatically fresh). If the common pipeline
raises before returning a trustworthy validation report, verification fails closed
with a controlled infrastructure/validation error, without inventing Rule FAIL,
Rule Evidence or a fresh report; the common engine itself remains unchanged.
Controlled verify exits:
`3` integrity/staleness, `4` scope, `5` validation; CLI syntax errors remain `2`.

**Always:** `deterministic_verification: true`,
`semantic_acceptance_verified: false`, `task_completion_claimed: false`.
Acceptance obligations and risks are counts/bound context only. No natural-language
criterion PASS/FAIL, criterion-to-test/Rule mapping, AI attestation, risk outcome,
human approval record or canonical task/completion object is created. PASS means
only that this deterministic contract passed, **not** semantic acceptance,
approval, completion, merge or shipping. No staging, commit, push, PR, finalization
or canonical mutation occurs. **Stage 14 remains the end-to-end completion gate.**

Stage 12 closure remains subject to independent owner/main-chat acceptance. This
candidate does not self-approve Stage 12C or declare Stage 12 complete.

### Stage 13A: Source Capture & Provenance

Stage 12 is independently accepted and published. The roadmap now assigns
External Source Intake & Canonicalization to Stage 13 and moves the former
bootstrap/end-to-end gate to Stage 14. Stage 13A, Stage 13B1 and Stage 13B2a are
independently accepted and published. Stage 13 remains open for 13B2b1/13B2b2,
13B3 and 13C.

**External Source != Proposal != Canonical Product Truth.** Source receipts
are durable canonical provenance facts. They neither become nor authorize
requirements, decisions, features, risks or implementation. `knowledge/**`
remains product truth; source material does not enter its object loader.

Small immutable files provide independent identities, append-only capture
history, individual validation/hashability, low merge contention and exact Git
history per capture. There is no central mutable `sources.yaml` registry.
The durable layer is neither tooling policy (`.project/**`) nor disposable
output (`.generated/**`):

```text
sources/definitions/SRC-<32 lowercase hex>.json
sources/captures/CAP-<32 lowercase hex>.json
sources/snapshots/CAP-<32 lowercase hex>/payload.bin  # explicit retention only
```

Both receipt contracts use strict packaged JSON Schemas and reject unknown
fields. Source Definition v1 has exactly:

```text
schema_version: 1
profile: project-system-source-v1
project_id, source_id, key, provider, kind
```

`key` is a unique project-local non-secret alias matching
`[a-z0-9][a-z0-9._-]{0,79}`. Provider matches `[a-z][a-z0-9_-]{0,63}`.
Kind is exactly `conversation|document|image|archive|design|other`.
Source identity payload contains only `project_id, key, provider, kind`.

Capture v1 has exactly:

```text
schema_version: 1
profile: project-system-source-capture-v1
project_id, capture_id, source_id, content_sha256, bytes,
media_type, retention, snapshot
```

Capture identity payload contains only `project_id, source_id, content_sha256,
bytes, media_type, retention`. IDs are respectively `SRC-` and `CAP-` plus the
first 32 lowercase hexadecimal characters of SHA-256 of the identity payload.
Identity JSON is UTF-8, sorted keys, compact separators `(',', ':')`,
`ensure_ascii=False`, `allow_nan=False`, no terminal LF. Stored receipts are
UTF-8, sorted keys, indent 2 and one terminal LF. Validation independently
recomputes IDs and requires exact filename/internal-ID agreement. Semantic
identity changes require a different ID; conflicting reuse of a Source key
fails. New content produces another capture of the same Source. Media type or
retention changes intentionally produce another capture identity.

Media types are normalized lowercase `type/subtype` values (1..127 ASCII token
characters per component, starting alphanumeric; remaining token characters
are alphanumerics and `!#$&^_.+-`). MIME parameters/whitespace are not accepted.
The CLI default is `application/octet-stream`; no MIME/content inference occurs.

```bash
project source capture PATH --key KEY --provider PROVIDER --kind document
project source capture PATH --key KEY --provider PROVIDER --kind document --media-type application/pdf --retention repository-snapshot
```

Default `reference` writes only the Source Definition and Capture receipt:
`snapshot` is null and validation needs no original source file. Explicit
`repository-snapshot` maps to internal `repository_snapshot`; snapshot metadata
is exactly `{path, sha256, bytes}`. Path is exactly
`sources/snapshots/<capture_id>/payload.bin`; hash/size equal capture content
hash/size. Input names and locations are never retained. Identical captures
reuse matching existing receipts without reserializing/replacing them. There
are no overwrite, update, delete or lifecycle-reset commands.

Input PATH may be external to the project. It must be a regular file with no
symlink/reparse traversal. Reads use 128 KiB chunks and the single named source
limit `MAX_SOURCE_BYTES = 256 * 1024 * 1024` (256 MiB); receipts are limited to
64 KiB. lstat/fstat identity, size, mtime and mode are checked before/open/during/
after reading, and counted bytes must match the stable size. A changing input
fails closed. Reference hashing does not copy raw bytes into the project.
Snapshot capture streams the same hashed bytes into an exclusive temporary
file, flushes/fsyncs and publishes it at the deterministic path, without a
second read of the potentially changed input. Publication uses a same-filesystem
atomic hard link with no replacement; filesystems without that operation fail
closed. Existing destinations are bounded-loaded/validated and must be
semantically equal (snapshot bytes must have the exact expected hash/size).
Temporary files are removed on ordinary failures, and only newly published
files still matching this invocation's inode are rolled back. No existing
receipt is silently rewritten. This is not an OS sandbox, a multi-file atomic
transaction or a guarantee against malicious concurrent filesystem replacement;
an interrupted process can leave artifacts that normal validation rejects.

`project validate` consumes source-layer inspection as part of its existing
validation report, so CI/generation/task/SYNC validation sees provenance errors
through the same pipeline. It checks bounded strict UTF-8 JSON, duplicate keys,
non-finite values, schemas, project identity, recomputed IDs, unique IDs and
Source keys, source references, exact snapshot metadata, safe regular snapshots
and their streamed hash/size. Orphan payloads and unexpected layout/files fail
with deterministically ordered ERROR issues; only empty regular `.gitkeep`
placeholders are ignored. No source content is executed.

New `project init` creates `sources/{definitions,captures,snapshots}/.gitkeep`
and adds `sources/snapshots/**` to `.llmignore`, not `.gitignore`. Existing
projects without a source layer remain valid without migration. Capture does
not silently edit their ignore configuration; owners choosing snapshot storage
in an existing project should explicitly exclude it from AI retrieval first.
Raw snapshots may contain customer, personal, commercial, confidential or other
sensitive material. Storage is explicit and durable; there is no encryption,
secret scanning or automatic disclosure-prevention claim. Keys/providers/media
types are public metadata aliases, and SHA/size receipts can reveal content
equality. Receipts contain no input absolute path/original filename, local
directory, username, host, timestamp, PID or random run identity. Git history
is the repository-history boundary.

13A performs no network, stdin ingestion, URL fetching, connectors, AI, OCR,
Telegram/HTML/PDF/image parsing, proposal extraction, accept/reject workflow,
canonical apply, product knowledge mutation, staging or commit/push. Source
bytes are opaque. Source validity does not establish source authenticity or
human approval. Stage 13B/13C own representation/extraction/proposal/audit/review/apply; Stage 14
remains the end-to-end AI development/completion gate.

### Stage 13B1: Source Representation & Evidence Anchors

Published Stage 13B1 adds deterministic representation metadata only:

```text
CAP receipt -> verified exact bytes -> utf8-lines@1 -> SEG/index -> REP receipt
```

Source, Capture, Representation, Proposal, Audit and Canonical Product Truth are
distinct authority layers. A REP is durable canonical provenance metadata with
`canonical_authority: false`; it cannot mutate or authorize `knowledge/**`,
`docs/**`, policies, Skills or implementation. No AI, proposal, XRUN, audit,
human review or canonical apply exists in 13B1.

Durable Git-tracked evidence contains metadata only:

```text
intake/representations/REP-<32 lowercase hex>.json
intake/representations/REP-<32 lowercase hex>.segments.jsonl
```

Rendered source bytes are disposable cache only:

```text
.generated/source-representations/REP-<id>/segments/SEG-<id>.txt
```

Durable receipts and JSONL contain IDs, hashes, byte offsets, line numbers and
adapter identity, never raw/rendered text, quoted customer content, original
filename/path, username, host, timestamp or PID. Deleting `.generated/**` does
not invalidate durable evidence; verified CAP bytes can deterministically
reproduce the same SEG IDs, index and REP ID before rebuilding cache.

Representation v1 has exactly `schema_version`, `profile`, `project_id`,
`representation_id`, `capture_id`, `capture_content_sha256`, `adapter`,
`segment_index`, and `canonical_authority`. The adapter is exactly:

```json
{"id":"utf8-lines","version":1,"options":{}}
```

Its fingerprint is full lowercase SHA-256 of canonical compact JSON for those
three fields. The fingerprint deliberately excludes Python source-file bytes:
behavior/output is independently bound by the exact durable segment-index hash.

Each descriptor has exactly `segment_id`, zero-based `ordinal`, one-based line
locator, half-open byte `source_span`, exact source hash/size, and exact rendered
hash/size. SEG identity excludes both REP ID and ordinal and hashes canonical
JSON containing `project_id`, `capture_id`, adapter fingerprint, locator, source
span and the four source/rendered hash/size fields. Thus there is no SEG/REP
identity cycle; ordinal is ordering metadata. REP identity hashes canonical JSON
containing project/capture identities, capture content hash, adapter fingerprint,
and exact index SHA/byte/descriptor count. All IDs use the first 32 lowercase
hexadecimal characters of SHA-256 prefixed by `SEG-` or `REP-`.

`utf8-lines@1` accepts strict UTF-8 and scans physical LF bytes. Each physical
line becomes one SEG. Its source span/hash includes LF and preceding CR for CRLF.
Rendered bytes remove only terminal LF or CRLF; lone/embedded CR, BOM, Unicode,
case and all other whitespace remain byte-exact. Blank physical lines are real
segments. A terminal LF creates no synthetic EOF segment; an empty source has
zero segments. Byte offsets count encoded bytes, not Unicode characters. The
durable JSONL is compact canonical UTF-8 JSON, one descriptor per LF-terminated
line in exact ordinal sequence; the empty index is zero bytes.

For a reference Capture, `project source represent CAP-ID --adapter utf8-lines
--input PATH` requires the exact external bytes. Stage 13A path/reparse/TOCTOU
checks stream them into a neutral temporary `verified.bin`, compare SHA/size to
the CAP receipt, and only then let the adapter read that temporary copy. For a
repository snapshot, `--input` is forbidden; the bound snapshot is validated
and copied through the same verified-byte boundary. Sensitive temporary bytes
remain in controlled OS temporary storage and are removed on success/failure.
No network, URL or stdin transport is supported.

Generation is bounded by `MAX_REPRESENTATION_SEGMENTS = 100_000` and
`MAX_SEGMENT_INDEX_BYTES = 32 * 1024 * 1024`; overflow fails without truncated or
partial durable REP state. Stage 13A's 256 MiB source bound remains unchanged.
Matching immutable receipt/index pairs are reused without rewritten bytes or
mtimes; deterministic-path conflicts fail closed. Missing/corrupt disposable
segment cache can be rebuilt after exact durable identity reproduction.

Normal `project validate` checks only durable representation provenance: strict
receipt schema/project/CAP/adapter/REP bindings, safe exact index path, stable
index hash/bytes/count, canonical strict UTF-8 JSONL, exact ordinals/line
locators, contiguous monotonic spans covering Capture bytes, SEG recomputation,
uniqueness and bounds. `.generated/**` is never required. A central intake
namespace inspector now owns known top-level siblings; the representation layer
owns only `intake/representations/**`. Unexpected `intake/**` files still fail.
Existing projects without `intake/**` remain valid and acquire no new path
requirements.

Validation cannot re-read unavailable reference content and therefore proves
the durable cryptographic/index contract rather than semantic meaning or source
authenticity. Reproduction happens during represent/rebuild. No PII/secret
detection is claimed. Stage 13B2b, 13B3 and 13C remain unimplemented; Stage 13
remains open and Stage 14 remains the end-to-end gate.

### Stage 13B2a: Extraction Contract & Deterministic Sealer

Published Stage 13B2a adds no semantic executor or model call. It accepts
untrusted JSON produced elsewhere and performs only deterministic validation,
normalization and immutable sealing:

```text
REP / durable SEG index
  -> XCON
  -> untrusted semantic submission
  -> deterministic normalization
  -> content-free Proposal manifest commitment
  -> XRUN -> PROP receipts
```

AI output is data, never authority. Source, Capture, Representation, Proposal,
Audit and Canonical Product Truth remain distinct. Every new receipt has
`canonical_authority: false`; XRUN and PROP also have
`human_review_required: true`. No knowledge comparison, canonical target, patch,
approval or mutable lifecycle status exists. Declared executor metadata records
caller-supplied provenance only and does not prove that a hosted provider or
model produced the submission.

The central intake inspector owns only top-level namespace safety. Known optional
roots are `representations`, `extraction-contracts`, `extraction-runs`,
`proposals`, and `extraction-submissions`. Unknown/non-directory/symlink/reparse
entries fail closed. Each sublayer alone validates its artifact semantics.
Legacy projects without `intake/**` and Stage-13B1-only projects remain valid.

Durable Git-tracked layout is:

```text
intake/extraction-contracts/XCON-<32hex>.json
intake/extraction-runs/XRUN-<32hex>.json
intake/proposals/PROP-<32hex>.json
intake/extraction-submissions/XRUN-<32hex>/submission.json  # explicit snapshot only
```

New projects create empty `.gitkeep` files for these roots and add
`intake/extraction-submissions/**` to `.llmignore`, not `.gitignore`. Existing
projects are not rewritten. The optional cache at
`.generated/source-extractions/<XRUN-ID>/submission.json` is disposable and is
never required by validation.

#### Extraction contract

`project source extraction contract REP-ID [--segment SEG-ID ...]
[--allow-kind KIND ...]` creates strict profile
`project-system-extraction-contract-v1`. Its exact fields are
`schema_version`, `profile`, `project_id`, `contract_id`, `representation_id`,
`presented_segment_ids`, `allowed_kinds`, derived `coverage`, and
`canonical_authority`. At least one presented SEG must exist in the referenced
durable REP index. Caller ordering is discarded: SEG IDs use REP ordinal order;
allowed kinds use the fixed registry order:

```text
requirement, decision, risk, question
```

Duplicates and unknown values fail rather than being deduplicated. With no SEG
flags all REP segments are used; zero-segment REPs fail. The contract is bounded
by 10,000 presented segments and 4 MiB. Coverage records presented count,
representation count and exact completeness, and is recomputed by validation.

XCON identity is `XCON-` plus the first 32 lowercase hexadecimal characters of
SHA-256 over canonical JSON containing exactly:

```text
profile, project_id, representation_id,
presented_segment_ids, allowed_kinds
```

Derived coverage is intentionally excluded. There is no semantic text in XCON.

#### Untrusted submission and normalization

The strict `project-system-extraction-submission-v1` input has exactly
`schema_version`, `profile`, and a non-empty `proposals` array. Every Proposal
input has exactly `kind`, `statement`, `support`, and `evidence_segment_ids`.
Authority/approval/target/patch/object/status fields are rejected, not stripped.
Kind must be globally known and allowed by XCON. Support is exactly
`explicit|inferred|ambiguous` and remains the executor's unverified declaration.
Statements are non-empty, not whitespace-only, and at most 8 KiB UTF-8; accepted
text is not trimmed, case-folded or Unicode-normalized. Evidence is non-empty,
unique, at most 64 IDs, restricted to XCON-presented SEG IDs, and normalized to
XCON order.

Raw input is bounded to 4 MiB before strict UTF-8 JSON parsing. Duplicate keys,
non-finite values, unknown fields, wrong types, empty input, more than 256
proposals and excess nesting/decoder failure fail closed. Each exact normalized
Proposal payload is canonical JSON of only its four allowed fields. Payload hash
is SHA-256 of those bytes. Proposal input order is non-authoritative: records sort
by `(payload_sha256, payload_bytes)`; identical normalized payloads are rejected.
The full canonical normalized submission is bounded to 2 MiB and its exact hash,
byte count and proposal count are sealed. That normalized submission SHA is the
commitment to the exact semantic normalized bytes.

#### XRUN and PROP

`project source extraction seal XCON-ID SUBMISSION_PATH --executor-kind ai
--provider PROVIDER --model MODEL --instruction-sha256 SHA256
[--retention reference|repository-snapshot]` safely reads the untrusted regular
file once through the Stage-13A lstat/fstat/TOCTOU boundary. It never verifies a
path and then reopens it for parsing, and private local paths do not enter
receipts or controlled errors. There is no network, SDK or model execution.

Executor fields are exactly `kind=ai`, bounded ASCII provider identifier,
bounded non-control model string, and lowercase instruction SHA-256. The sealer
profile is `project-system-extraction-sealer-v1`.

XRUN v1 has exactly `schema_version`, `profile`, `project_id`, `run_id`,
`contract_id`, `representation_id`, `sealer_profile`, `executor`, `submission`,
`proposal_ids`, `canonical_authority`, and `human_review_required`. XRUN identity
excludes PROP IDs to avoid a cycle and hashes canonical JSON containing exactly:

```text
project_id, contract_id, representation_id, sealer_profile, executor,
submission_sha256, submission_bytes, proposal_count,
proposal_manifest_sha256, retention
```

`submission` contains exactly `sha256`, `bytes`, `proposals`,
`proposal_manifest_sha256`, `retention`, and `snapshot`. Before XRUN identity is
computed, the sealer builds the exact content-free manifest profile
`project-system-proposal-manifest-v1` in normalized Proposal order. Each manifest
entry contains only `payload_sha256`, positive `payload_bytes`, and normalized
`evidence_segment_ids`. SHA-256 over canonical JSON of that exact manifest is
`proposal_manifest_sha256`; the manifest itself is not persisted. It contains no
statement, kind, support, PROP/XRUN ID, target, approval, path, or timestamp.
Thus the identity DAG is normalized Proposal payloads -> content-free manifest
-> manifest hash -> XRUN ID -> PROP IDs, with no XRUN/PROP cycle.

PROP v1 contains no statement or kind. Its exact fields are `schema_version`,
`profile`, `project_id`, `proposal_id`, `run_id`, `payload_sha256`,
`payload_bytes`, normalized `evidence_segment_ids`, `canonical_authority`, and
`human_review_required`. PROP identity hashes canonical JSON containing exactly
`project_id`, `run_id`, payload hash/bytes and evidence IDs. It has no ordinal,
target or approval state.

Default `reference` retention stores no durable semantic payload. The manifest
hash is the content-free durable bridge from the sealing result to the exact
PROP receipt commitments: validation reconstructs it from listed PROP payload
hash/bytes and evidence IDs, but cannot reconstruct or claim knowledge of the
unavailable semantic statements. Explicit `repository-snapshot` stores the exact normalized
canonical submission, never raw provider transport output, under its XRUN path;
snapshot hash/bytes equal XRUN submission metadata. Retention participates in
XRUN identity because these storage contracts differ. Snapshot content can be
sensitive durable Git data and has no provider-authenticity claim. Snapshot
validation additionally reconstructs the same manifest directly from the exact
normalized records before reproducing the exact PROP receipts and IDs.

Publication preflights deterministic destinations and uses same-filesystem
no-clobber hard links. Matching immutable artifacts are reused; contradictions
fail. On ordinary failure only files created by that invocation and still
matching its device/inode are rolled back. This is not a filesystem transaction:
a crash can leave partial immutable state, which normal validation rejects.
Missing/corrupt disposable normalized cache can be rebuilt only after the same
normalized submission, XRUN and PROP identities are reproduced.

Normal `project validate` verifies strict schemas, filenames/IDs, project and
REP/XCON/XRUN/PROP bindings, canonical orders, bounds, coverage, identities,
proposal lists, the XRUN-bound Proposal manifest commitment, orphan/partial state
and optional snapshot canonical bytes and semantic-to-PROP correspondence.
Unexpected files fail closed with deterministic
issue ordering. `.generated/**` is ignored. Stage 13B2b semantic execution,
Stage 13B3 independent audit and Stage 13C human review/apply remain open.

### Stage 13B2b1: Verified Extraction Pack

This Stage 13B2b1 candidate adds only deterministic preparation and independent
verification of a local, disposable Verified Extraction Pack. It performs no AI
or provider execution, network request, prompt assembly, Skill execution,
semantic extraction, Proposal generation, audit, approval, or canonical write:

```text
XCON.presented_segment_ids
  -> validated durable REP receipt and segment index
  -> exact rendered SEG cache paths
  -> safe byte reads plus hash/size verification
  -> canonical XPACK under .generated/source-extraction-packs/
```

`project source extraction pack create XCON-ID` never enumerates generated files
to discover authority. XCON supplies the exact SEG set; durable REP descriptors
supply locator, rendered SHA-256 and byte count. Each specific rendered cache is
read through the existing symlink/reparse/regular-file/TOCTOU-aware streaming
boundary, verified against that descriptor, decoded as strict UTF-8 and copied
without whitespace or Unicode normalization. Empty rendered segments remain
present. Missing or corrupt caches fail closed; Stage 13B2b1 does not reopen an
external reference source or rebuild Stage 13B1 caches. Stale unrelated cache
files cannot broaden the pack.

The strict profile is `project-system-verified-extraction-pack-v1`. Its exact
mandatory fields are `schema_version`, `profile`, `project_id`, `contract_id`,
`representation_id`, `segment_index_sha256`, `allowed_kinds`, `segments`, and
`canonical_authority=false`. Every segment contains exactly `segment_id`, the
durable line `locator`, `rendered_sha256`, nonnegative `rendered_bytes`, and
`text`. Segment order equals XCON order and `allowed_kinds` equals XCON exactly.
It contains no pack ID, model/provider identity, prompts, executable
instructions, approval/review state, canonical target, status, private path,
timestamp, or randomness.

Identity uses the existing compact canonical JSON convention, with no terminal
newline or self-reference:

```text
pack_bytes  = canonical_json(pack_document)
pack_sha256 = SHA256(pack_bytes).hexdigest()
pack_id     = "XPACK-" + pack_sha256[:32]
```

The full digest and exact byte count appear only in CLI metadata. The pack is
stored atomically at
`.generated/source-extraction-packs/XPACK-<32hex>/pack.json`; no XPACK receipt is
created in `intake/**`. A completed temporary file is flushed/fsynced on the same
filesystem and published with an atomic no-clobber hard link. Success statuses
are only `created` and `existing`; safely read exact matching bytes are reused
without rewriting. If a destination appears at publication, creation fails
without overwriting it. A corrupt existing cache fails closed and is neither
removed, renamed nor overwritten: explicit removal by the user or controlling
process is required before recreation. There is no automatic `rebuilt_cache` or
cache deletion command. Filesystems without hard-link support fail closed;
publication never falls back to a clobbering operation. Unsafe paths and observed
identity changes fail closed. These guarantees do not cover arbitrary concurrent
parent-directory mutations or subsequent modification of successfully published
or reused files. Verification before semantic execution remains mandatory.
Normal `project validate` ignores this derived cache, so deletion does not
invalidate the project.

`project source extraction pack verify XPACK-ID` safely rereads exact bytes,
rejects duplicate keys/non-finite values/unknown fields/noncanonical JSON,
recomputes the full hash and ID, and reloads the valid durable XCON, REP and
segment index. It proves exact project/contract/representation/index/kind/SEG
membership, count, order, locator, hash and byte bindings, then re-encodes each
text value to verify its durable rendered commitment. Verification is independent
of rendered SEG caches and original source files; future semantic execution must
repeat it immediately before use rather than trust an earlier result.

Hard ceilings are 512 segments, 256 KiB per rendered segment and 2 MiB for exact
canonical pack bytes. They are construction limits, not token estimates. There
is no truncation, subset selection or batching; a smaller pack requires a more
narrowly scoped XCON.

XPACK may contain confidential, personal or secret source text and remains
local. It is excluded from normal AI retrieval with `.generated/**`; there is no
automatic transfer or redaction. Source instructions are untrusted bytes and are
never interpreted or executed by the builder. Provenance of bytes is not
semantic correctness or prompt-injection resistance.

Published XRUN identity does not contain XPACK ID, and sealing an XRUN therefore
does not prove which pack was delivered to a model. Stage 13B2b2 must add a
separately verifiable, explicitly authorized execution-provenance link without
changing published XRUN identity. This Stage 13B2b1 candidate does not implement
that link, self-approve Stage 13B2b1, or complete Stage 13B2/Stage 13.

### Stage 13B2b2-A candidate: execution contract and crash safety

This optional foundation follows the published Stage 13B2b1 XPACK contract.
It is a candidate for independent review, not acceptance of Stage 13B2b2 or
completion of Stage 13. Package version stays 0.12.1. There is no XINV, response
sealing, XRUN integration, real provider, local model, network request, CLI
execution command or canonical write. The bounded Python API is test-only.

The central intake owner registers `intake/executions/`; initialization creates
its empty `.gitkeep`, and absent execution namespaces remain valid for legacy
projects. Only this execution layer inspects the attempt children. Existing
SRC/CAP/REP/SEG/XCON/XPACK/XRUN/PROP schemas, identities and normalization are
unchanged. New modules and the strict `execution-attempt.schema.json` use existing
package/module discovery and recursive packaged schema assets.

#### Attempt and exact authorization contract

Each invocation of `prepare_attempt` creates `ATTEMPT-<32 lowercase hex>` using
128 bits of cryptographically secure randomness, not a hash of shared inputs.
Collisions never authorize replacement. Each attempt has its own directory:

```text
intake/executions/ATTEMPT-.../
  01-prepared.json       PREPARED
  02-boundary.json       DISPATCH_INTENT or DISPOSITION(abandoned)
  03-disposition.json    optional DISPOSITION(unresolved), only after intent
```

Checkpoints are strict canonical UTF-8 JSON, at most 64 KiB, with schema/profile,
project/attempt identity, state, full previous-checkpoint SHA-256 and full
checkpoint SHA-256 (the existing canonical JSON serialization, excluding only
the checkpoint's own digest). They are monotonic immutable files; even identical
existing bytes are a conflict, never a newly acquired dispatch claim.

PREPARED binds project/attempt, exact XCON ID and SHA-256 of the entire durable
XCON receipt, exact XPACK ID and full canonical byte SHA-256/count, trusted
instruction SHA-256/count, adapter ID/version, model, options, execution mode,
destination, limits, retention and extraction-only scope. No raw instructions,
source segments or pack text are retained in execution metadata. The caller of
this test API supplies trusted instruction bytes; no project/source instruction
is promoted to trusted authority. Every field is inside the contract commitment.

Only adapter `deterministic-fake` version `1`, model `fake`, mode `local_fake`,
destination `none`, retention `metadata_only` and scope `extraction_only` are
allowed. Limits are bounded to 2 MiB pack, 64 KiB response/instruction and 30
seconds declared timeout. Fake timeout is simulated without a wait; these limits
do not certify a future provider's timeout implementation. Unknown options and
any local-to-remote escalation are rejected before durable publication.

`authorize_test_attempt` requires an explicit expected full contract digest and
independent revalidation. It issues an in-memory process-local HMAC capability
bound to that exact contract (including unique attempt). A fresh process must
explicitly reauthorize. Caller `approved=true`, a dict or executor assertion is
not a capability. Changed parameters cannot reuse one. The sole dispatch route
uses the fixed fake adapter, never a caller-supplied provider. Durable intent
records `kind=test_only`, the contract digest and authorization-record digest.
Independent validation verifies those commitments, not production human approval
or authenticity against a malicious party capable of recomputing all receipts.
This is expressly **test harness authorization**, not a human approval engine.
Trustworthy production authorization remains unresolved for a subsequent stage.

#### Publication, concurrency and threat model

Completed bytes are flushed/fsynced in a unique temporary regular file under
`.generated/execution-staging/` on the destination filesystem; publication uses
`os.link` atomically without clobber, followed by safe independent reread. No
`replace`, check-then-overwrite or copy fallback is permitted. Unsupported links,
including cross-device publication, fail closed before transport. Only the
creator of `02-boundary.json` may invoke transport. An existing boundary, even
identical, never permits redispatch. Concurrent abandon/dispatch operations use
the same slot so both cannot win. After intent, an explicit unresolved disposition
may be appended; it never asserts transport cancellation or response success.

Existing lexical path inspection and bounded stable-file reads reject traversal,
symlinks, Windows reparse points, nonregular artifacts and conflicting receipts.
The model covers cooperative processes and process crashes on a filesystem with
atomic hard links, with trusted root/ancestor directories and no hostile removal
or rewrite of published checkpoints. It is not an OS sandbox, remote transaction,
or protection against a malicious same-user filesystem writer. In particular,
deleting a dispatch claim defeats its history; hashes do not prevent deliberate
wholesale metadata replacement. Windows is a required target: NTFS hard-link
claims and process-crash tests must run on the actual test volume; other volumes
need their own verification. File fsync does **not** establish universal
directory-entry durability across power loss. Orphan temporary files after a
hard process crash are disposable generated state, never execution Evidence.

#### Independent recovery and terminal policy

`inspect_attempt` independently checks strict schema, exact paths/types,
checkpoint hashes, project/attempt bindings, full XCON bytes and published XCON
validation, contract/authorization commitments and legal predecessor/state links.
The persisted XPACK digest/ID binding is checked without requiring disposable
caches. Dispatch separately rereads and independently verifies exact XPACK bytes
and the published XPACK/XCON/REP/SEG bindings immediately before acquiring intent.

| Durable state | Recovery | Explicit permitted action |
|---|---|---|
| PREPARED | Valid but incomplete; no automatic dispatch | Revalidate and explicitly test-authorize; dispatch or append abandoned |
| DISPATCH_INTENT, no recorded response | Delivery unknown, even if fake success was observed | Never retry this attempt; explicitly append unresolved |
| DISPOSITION abandoned | Terminal provenance, no semantic Evidence | No dispatch and no checkpoint rewrite |
| DISPOSITION unresolved | Terminal provenance, no semantic Evidence | No retry and no checkpoint rewrite |
| Empty ATTEMPT directory, no PREPARED | Diagnosed orphan; ERROR, execution history unverified | Explicit operator intervention; no automatic recovery, dispatch or cleanup |
| Missing initial/required predecessor, corrupt/conflicting/unexpected artifact | Invalid, fail closed | Report; never fabricate recovery or delete receipts to pass |

An interruption after directory creation but before atomic PREPARED publication
can leave an empty durable attempt directory and an orphan generated staging file.
Independent inspection explicitly diagnoses that empty directory as an orphan
requiring operator intervention, not a valid interrupted PREPARED attempt and not
semantic Evidence. Neither execution nor nonexecution is inferred from absent
receipts: a benign pre-publication crash cannot be distinguished from deletion of
earlier checkpoints. Authorization, dispatch and terminal closure all fail closed.
Nonempty directories without PREPARED remain invalid; unknown artifacts remain
rejected. Generated temporary bytes are never promoted to a durable checkpoint.
Stage A supplies no automatic cleanup, reconstruction or retry mechanism. Operator
intervention must review/preserve the artifacts and authorize any separate bounded
remediation; it does not mean deleting or overwriting receipts to pass validation.
The checkpoint layout and publication algorithm are unchanged.

RESPONSE_RECORDED and response sealing are deferred to Stage B. Stage A retains
no response bytes/hash receipt and never claims byte replay or extraction success.
Fake observations exist only in the caller's transient report. Success, pre-intent
failure, post-intent failure, timeout, unknown delivery, malformed response and
interruption after observation exercise orchestration only. No source is sent or
semantically processed by the fake transport. Fault-injection seams are for tests,
not caller authorization extensions.

Project validation reports valid incomplete attempts as BLOCKING, malformed
attempts as ERROR and explicit terminal abandoned/unresolved as WARNING. Existing
task-completion validation therefore cannot silently pass an unfinished attempt.
Terminal records are also never accepted as successful extraction/task Evidence.
`close_test_attempt` is narrowly test-only terminal provenance, not another generic
approval/task lifecycle. A new execution after unknown delivery, if ever allowed
in a later production stage, must be a distinct explicitly authorized attempt and
must not imply that remote duplication is impossible.

## Normative v1 field contract

This section is normative for the initial JSON schemas and Rule Engine implementation.
Fields not listed here are not part of Rules v1 unless this architecture contract is explicitly revised.

### Rule registry root

Required root fields:

```text
schema_version
profile
rules
```

`schema_version` must equal `1`.
`profile` must equal `project-system-rules-v1`.
`rules` is a mapping keyed by Rule ID.
Unknown root fields are rejected.

### Rule ID

A Rule ID must match the stable form `<PREFIX>-<NUMBER>`.

Examples:

```text
ARCH-001
SEC-004
TEST-007
REPO-012
```

The prefix uses uppercase ASCII letters or digits, begins with a letter, and the numeric suffix contains at least three digits.

Rule IDs must be unique and must not collide with canonical knowledge-object IDs.

### Rule fields

Every Rule v1 requires:

```text
title
status
category
description
verification
enforcement
exception_policy
```

Optional Rule v1 fields are:

```text
scope
traceability
superseded_by
```

Unknown rule fields are rejected.

`title` and `description` must be non-empty strings.

`status` must be one of:

```text
draft
active
deprecated
```

`category` must be one of:

```text
architecture
repository
dependency
security
requirement
testing
process
```

`superseded_by` is optional and references another Rule ID.
It is intended for deprecated rules and must not reference the same Rule ID.

Only `active` rules participate in enforcement.
Draft and deprecated rules remain visible for definition validation and traceability but do not participate in gates.

### Verification fields

`verification` always requires:

```text
method
```

`method` must be exactly one of:

```text
deterministic
ai
human
```

For `deterministic`, `checker` is required and `parameters` is optional.

For `ai` and `human`, `checker` and `parameters` are forbidden in Rules v1.

`checker` must reference an allowlisted packaged checker ID.

`parameters`, when present, must be a mapping accepted by the selected checker contract.

Unknown verification fields are rejected.

### Scope fields

`scope` is optional.

When `scope` is absent, the rule has project-wide scope subject to checker semantics.

Rules v1 supports:

```text
scope.paths
```

`paths` must be a non-empty unique list of repository-relative path patterns.

Absolute paths, parent traversal, and `.git/**` scope are forbidden.
Paths use forward-slash canonical form in policy regardless of host operating system.

Unknown scope fields are rejected.

Rules v1 intentionally does not introduce a general `applies_when` expression language.
Applicability is derived from checkpoint, resolved scope, Evaluation Context, and bounded checker semantics.

### Enforcement fields

`enforcement` requires:

```text
severity
checkpoints
```

`severity` must be one of:

```text
BLOCKING
ERROR
WARNING
INFO
```

`checkpoints` must be a non-empty unique subset of:

```text
project_validate
task_verify
sync_verify
```

Unknown enforcement fields are rejected.

### Traceability fields

`traceability` is optional and may contain:

```text
object_ids
docs
```

Both are optional unique lists.

`object_ids` references canonical knowledge-object IDs.
`docs` contains canonical repository-relative document paths.

Traceability does not grant authority and does not replace reference validation.

Unknown traceability fields are rejected.

### Exception policy

`exception_policy` must be one of:

```text
forbidden
decision_required
```

`forbidden` means no project exception may waive a FAIL for the rule.

`decision_required` permits FAIL -> WAIVED only through a valid exception record linked to a canonical Decision.

### Exception registry root

Required root fields:

```text
schema_version
profile
exceptions
```

`schema_version` must equal `1`.
`profile` must equal `project-system-rule-exceptions-v1`.
`exceptions` is a mapping keyed by Exception ID.
Unknown root fields are rejected.

### Exception ID

Exception IDs use the existing durable-object style:

```text
EXC-YYYYMMDD-xxxxxxxx
```

where the suffix is eight lowercase hexadecimal characters.

Exception IDs must be unique.

### Exception fields

Every Exception v1 requires:

```text
rule_id
state
mode
reason
scope
decision_id
approved_by
approved_at
```

Optional Exception v1 fields are:

```text
expires_at
revoked_by
revoked_at
revocation_reason
```

Unknown exception fields are rejected.

`rule_id` must reference an existing Project Rule.

`state` must be one of:

```text
active
revoked
```

Expiration is derived from `expires_at`; `expired` is not a manually assignable state.

`mode` must be one of:

```text
temporary
permanent
```

`reason`, `approved_by`, and `decision_id` must be non-empty.
`approved_at` must be a valid date-time.

`scope` is mandatory for every exception and follows the same safe path rules as Rule scope.
An exception with broader scope than the affected violation must not apply outside its explicit scope.

Exception scope matching is anchored to the repository root and consumes the
complete canonical POSIX path. `*`, `?`, and character classes such as `[ab]`
match only within one path segment. A segment that is exactly `**` matches zero
or more complete segments. Thus `docs/*.md` does not match
`docs/nested/file.md`, while `docs/**/*.md` matches both `docs/file.md` and
`docs/nested/file.md`. Backslashes, absolute paths, parent traversal, and
`.git/**` remain invalid.

For a checker result that identifies multiple violating paths, one exception
applies only when its scope covers every violating path. Partial coverage never
produces a partial waiver. Repository path checkers use their reported path;
knowledge field checks resolve every reported violating object ID to the
canonical object path already present in Evaluation Context. Exception
resolution does not re-read the filesystem.

Temporary exceptions require `expires_at`.
Permanent exceptions must not contain `expires_at`.
Resolution receives an explicit timezone-aware `as_of` value. A temporary
exception is expired when `as_of` is equal to or later than `expires_at`.

For `state: revoked`, `revoked_by` and `revoked_at` are required.
For `state: active`, revocation metadata is forbidden.

`decision_id` must reference an existing canonical Decision object.

### Effective exception semantics

An exception applies only when all of the following are true:

```text
the referenced rule exists
the rule is active
the rule exception_policy is decision_required
the exception state is active
the exception is not expired
the linked Decision reference is valid
the violation falls inside exception scope
the raw result is FAIL
```

Only then may effective status become WAIVED.

If more than one exception is applicable to the same raw FAIL result,
resolution fails closed as ambiguous; it does not choose by registry order.

ERROR, PENDING, PASS, and NOT_APPLICABLE are never transformed to WAIVED.

### Gate semantics

For an active rule at one of its configured checkpoints:

```text
PASS            -> non-blocking
NOT_APPLICABLE  -> non-blocking
WAIVED          -> non-blocking but explicitly reported
FAIL            -> blocking when severity is BLOCKING or ERROR
FAIL            -> non-blocking when severity is WARNING or INFO
ERROR           -> always blocking
PENDING         -> blocking when severity is BLOCKING or ERROR
PENDING         -> non-blocking when severity is WARNING or INFO
```

This ensures that missing required AI or human evidence cannot become implicit success.

### Schema strictness

Rules v1 schemas should reject unknown fields wherever practical.

Schema validation establishes structural validity only.
Semantic validation remains responsible for cross-references, checker existence, checker-parameter contracts, safe normalized paths, lifecycle constraints, exception applicability, and collisions with existing Project System identities.
