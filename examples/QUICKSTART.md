# Quickstart

```bash
python -m pip install --no-build-isolation -e /path/to/Project_Template_v1.1_RC2
project init MyApp --path ./my-app --type mobile_app --governance solo
cd my-app
project new feature --title "Onboarding" --domain product --owner owner
project validate
project generate
project context project --budget small
```

For semantic work, generate a bounded task pack:

```bash
project task FEAT-YYYYMMDD-XXXXXXXX --budget medium --mode implement
project sync DEC-YYYYMMDD-XXXXXXXX --budget medium
```

Give the resulting `.generated/context/.../context.md` and `manifest.json` to ChatGPT, Codex, Gemini, a local model, or a human reviewer.
