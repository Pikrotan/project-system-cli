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
13. Bootstrap and end-to-end AI Development Gate
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
