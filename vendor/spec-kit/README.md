# Vendored spec-kit prompts

Source: https://github.com/github/spec-kit — pinned at tag **v0.12.11** (2026-07-10).

- `checklist.md` — `templates/commands/checklist.md` ("unit tests for English": checklists that test requirements quality, not implementation).
- `analyze.md` — `templates/commands/analyze.md` (non-destructive cross-artifact consistency analysis).

These are reference copies, MIT-licensed upstream. The factory does not execute them
directly: `roles/spec-linter.md` is the adapted, factory-owned prompt that runs in the
pipeline. When upgrading the pin, re-diff `roles/spec-linter.md` against these files and
carry over anything genuinely better; never point `run-agent.sh` at these files as-is
(they assume spec-kit's `.specify/` project layout and extension hooks).

To refresh at a new tag:

```bash
curl -s -o checklist.md "https://raw.githubusercontent.com/github/spec-kit/<tag>/templates/commands/checklist.md"
curl -s -o analyze.md   "https://raw.githubusercontent.com/github/spec-kit/<tag>/templates/commands/analyze.md"
```

Then update the tag in this README and in `roles/spec-linter.md`'s provenance note.
