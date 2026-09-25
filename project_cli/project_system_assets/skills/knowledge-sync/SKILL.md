---
name: knowledge-sync
description: Synchronize already approved changes into canonical project knowledge and narratives within a prepared write scope; do not use for approval, brainstorming, implementation, or release.
---

# Knowledge Sync

## Use this skill when

Apply an already human-approved change through a prepared task or SYNC pack and make the authorized atomic objects and narrative documents consistent.

## Authority and scope

Treat `knowledge/**` and `docs/**` as canonical state, policies and schemas as constraints, and `.generated/**` as deterministic output. Work only inside the effective task scope. Approval metadata may be copied only from supplied evidence. Proposal and unresolved input never authorizes active canonical truth.

## Workflow

1. Load the prepared context and allowed write set.
2. Preserve object IDs, provenance, lifecycle links and approved meaning.
3. Update only the affected canonical objects and narratives; remove stale conflicting text within scope.
4. Run the declared deterministic validation or SYNC verification capability.
5. Present the diff and unresolved conflicts for human review.

## Stop and escalate when

Stop for missing approval evidence, an out-of-scope impact, contradictory active state, a required new decision, or a validation failure that needs semantic interpretation.

## Non-responsibilities

Do not infer decisions from discussion, expand scope, edit generated evidence, implement code, commit, push, or import Google/Figma content as canonical truth.
