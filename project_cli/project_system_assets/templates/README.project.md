# {{PROJECT_NAME}}

This repository uses Project Template v1.1.

## Start here

```bash
project validate
project skills validate
project generate
project context project --budget small
```

Canonical intent lives in `docs/` and active objects under `knowledge/`. Project workflows live under `.agents/skills/` and are registered by `.project/skills.yaml`; they do not replace canonical truth or human approval. `.generated/` is disposable.

`sources/definitions/` and `sources/captures/` hold immutable external-source
provenance, not product truth. `project source capture <PATH> --key KEY --provider
PROVIDER --kind document` records a hash/size receipt by default. Only explicit
`--retention repository-snapshot` stores raw bytes under `sources/snapshots/`.
Snapshots may contain sensitive customer/personal/commercial data; they are
excluded by `.llmignore` but remain Git-trackable. No encryption, secret scanning,
content interpretation or automatic knowledge mutation is provided. `project
validate` checks receipts and snapshot integrity.

`project source represent CAP-ID --adapter utf8-lines [--input PATH]` creates
durable text-free line evidence under `intake/representations/` and disposable
rendered segment cache under `.generated/`. Reference captures require exact
re-supplied bytes via `--input`; repository snapshots forbid it. Representation
metadata has no product authority and performs no semantic extraction or apply.
