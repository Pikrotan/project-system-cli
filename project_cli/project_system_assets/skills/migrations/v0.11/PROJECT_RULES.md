# Project Rules

The repository is a Git-native project knowledge system. `main` is the canonical repository state; each object's `status` determines whether it is current truth.

Human approval is required according to `.project/policies/governance.yaml`. In solo mode this is procedural; strict technical enforcement requires protected branches / required human review in the hosting platform.

Atomic objects carry lifecycle facts. Narrative docs explain intent and system behavior. Generated files are disposable. If generated output conflicts with canonical source data, regenerate it rather than editing the generated file.

Figma is visual truth where referenced. Google Docs, if used, is a projection. Structured design-change input is not automatically an approved product change.

Sync means making affected canonical material consistent after an approved change, not appending text everywhere. Deterministic tooling determines scope and validity; semantic editors work inside that scope; humans approve changes of meaning.
