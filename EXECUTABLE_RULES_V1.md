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
