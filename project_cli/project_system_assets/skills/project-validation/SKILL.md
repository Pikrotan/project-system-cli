---
name: project-validation
description: Run and interpret deterministic Project System validation, generation and health checks without claiming semantic correctness or silently repairing canonical content.
---

# Project Validation

## Use this skill when

Check schema, object-layer, graph, reference, generated-output and configuration integrity.

## Authority and scope

The CLI owns validation and generation. Derived outputs are command-owned and are not semantic Skill write authority. A passing validator does not prove human approval or semantic correctness.

## Workflow

1. Run the requested deterministic validation sequence.
2. Preserve exact errors, warnings and object counts.
3. Confirm generated output changed no canonical paths.
4. Classify failures by their actual deterministic boundary.
5. Report; do not repair semantic content unless a separate authorized task requests it.

## Non-responsibilities

Do not approve, reinterpret product meaning, weaken schemas, suppress failures, or edit canonical files merely to obtain a pass.
