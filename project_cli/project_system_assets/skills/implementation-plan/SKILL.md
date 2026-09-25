---
name: implementation-plan
description: Produce a bounded implementation plan from approved requirements and architecture without editing product truth or implementing code.
---

# Implementation Plan

## Use this skill when

Turn approved canonical scope into an ordered, testable implementation plan.

## Authority and scope

Planning does not authorize canonical edits or code changes. Treat missing product or architecture decisions as blockers rather than silently resolving them.

## Workflow

1. Load approved requirements, architecture, dependencies and relevant code context.
2. Map requirements to implementation areas, tests, migrations and rollout checks.
3. Order work by dependency and risk.
4. Identify decision gaps and validation gates.
5. Return a plan in the task response or prepared noncanonical context.

## Non-responsibilities

Do not implement code, change canonical knowledge, approve scope, create architecture truth, commit, push or release.
