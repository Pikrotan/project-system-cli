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

`project source extraction contract REP-ID` creates an immutable allowlist of
presented SEG evidence and proposal kinds. A separate external executor may
produce strict JSON; `project source extraction seal XCON-ID SUBMISSION_PATH
--executor-kind ai --provider PROVIDER --model MODEL --instruction-sha256 SHA256`
normalizes and seals it into non-authoritative XRUN/PROP receipts. The CLI makes
no model call; XRUN binds a content-free manifest hash of the exact durable PROP
commitments. The CLI performs no canonical reconciliation and requires later audit and
human review. Default retention stores no durable semantic statement; explicit
`--retention repository-snapshot` stores normalized JSON under sensitive
`intake/extraction-submissions/`, excluded from AI retrieval by `.llmignore`.

`project source extraction pack create XCON-ID` builds a local, disposable
Verified Extraction Pack from only the XCON-authorized rendered SEG caches after
checking their durable REP hash/byte commitments. `project source extraction pack
verify XPACK-ID` independently verifies its canonical bytes without requiring the
rendered cache or original source. XPACK may contain sensitive text, remains under
`.generated/**`, is never canonical truth, and is not sent to any provider.
Creation uses atomic no-clobber hard links and returns `created` or `existing`.
Corrupt XPACK cache requires explicit removal before recreation; automatic rebuild
is unavailable. Unsupported hard links fail closed. Reverify before semantic use;
publication/reuse does not prevent later edits or arbitrary parent-directory races.
