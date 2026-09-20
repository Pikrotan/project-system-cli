# Design Changes — designer guide

Use the Design Changes sheet when a meaningful design change should be visible
to the project owner. You do not need to know Git, YAML, command-line tools or
Project System internals.

## Your workflow

```text
Work in Figma
  -> add one Design Changes row for a meaningful change
  -> wait for Project System status
  -> answer an owner question if needed
  -> read refreshed Design Knowledge after approval
```

Fill in:

- **Date** — when the change was made.
- **Author** — your name.
- **Project Area** — the relevant part of the product.
- **Screen / Flow** — where users experience the change.
- **Change Type** — for example layout, state, navigation or content.
- **What Changed** — a clear text explanation. This is required.
- **Why** — the reason or expected benefit.
- **Figma URL** — optional HTTPS link to the relevant Figma location.
- **Logic Changed?** — choose `Yes`, `No` or `Unsure`.

A Figma link without **What Changed** is not enough. Use `Unsure` whenever you
cannot confidently say whether behavior, permissions, data, navigation or the
user flow changed.

Do not edit the columns starting with **Change ID**. Project System fills those
in and protects them where Google Sheets permits.

## Statuses

- **IMPORTED — OWNER REVIEW**: your description was safely recorded; it has not
  changed the product automatically. **Decision Needed?** remains `PENDING`
  until owner-controlled review, even when **Logic Changed?** is `No`.
- **IN REVIEW**: an owner-controlled change package is being reviewed.
- **APPLIED**: the approved change was completed; references appear alongside it.
- **REVIEWED — NO CANONICAL CHANGE**: the owner confirmed that no project
  knowledge update was required, which is common for visual-only refinements.
- **REJECTED / ABANDONED**: the owner closed the proposal without application.
- **ERROR**: correct the stated field and leave the Change ID intact.
- **CONFLICT**: the row changed after import, was changed during processing or
  has a duplicate identity. Ask the owner before editing further.

Changing a row never directly changes the product's approved knowledge. Product
logic, UX logic, data, permissions, flows and functionality always cross the
owner approval boundary. Design Knowledge is a read-only view; manual edits to
that Doc may be replaced by the next projection.
