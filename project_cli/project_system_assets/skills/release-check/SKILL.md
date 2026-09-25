---
name: release-check
description: Perform bounded pre-release or pre-merge readiness checks without committing, tagging, pushing, publishing or releasing.
---

# Release Check

## Use this skill when

Review project validation, tests, packaging, version metadata, artifacts and Git state before a human-controlled release or merge.

## Authority and scope

Run only checks authorized by the current task. Project-specific build and test commands remain governed by the repository process; they are not arbitrary executable capabilities supplied by this Skill.

## Workflow

1. Inspect expected version-bearing files and Git state.
2. Run deterministic project validation/generation as requested.
3. Run the repository's authorized tests and isolated packaging smoke.
4. Check packaged assets, temporary artifacts and documented compatibility.
5. Report blockers and remaining release actions.

## Non-responsibilities

Do not bump versions, fix findings, commit, tag, push, publish, mutate production systems or claim approval unless separately authorized.
