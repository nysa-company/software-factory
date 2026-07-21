# Software Factory Kit

Product-agnostic kit for running an AI software factory. Read `docs/product-brief.md`, `docs/architecture.md`, and `docs/factory-setup.md` before changing the engine model, role contracts, ticket flow, or product-instantiation contract.

## Run checks

- Full suite: `bash ci/test-all.sh`
- Dynamic preview: `bash ci/test-all.sh --changed origin/main HEAD`; required CI remains in shadow/full mode until its evidence gate passes.
- Target the smallest relevant check while iterating, then run managed local readiness; GitHub owns complete-suite verification.

## Conventions

- Keep the kit product-agnostic; Nysa-specific state belongs in the Nysa product repository.
- Reuse the existing shell and Python helpers before adding dependencies.
- Preserve the builder/test-author separation and evidence-first approval flow.
- Update the relevant file under `docs/` and `context/memory.md` when durable product or engine truth changes.

## Session end

Append an entry to `context/memory.md` for significant decisions, reversals, incidents, or system changes. Keep its Current truth section synchronized.

## Git (team standard)

- Branch before committing if on the default branch — never commit directly to it.
- On non-default branches, commit at logical checkpoints and at session end without asking. Never push, merge, or open a PR unless the user explicitly asks.
- Commit messages: a concise summary line that explains the *why*.
- Use the `gh` CLI for GitHub operations (PRs, issues).

## Never expose credentials (team standard)

- Never commit secrets. `.env` and key files stay gitignored; only `.env.example` is committed.
- When printing back any config, env vars, JSON, or logs, redact every value whose KEY matches `key|token|secret|password|url|dsn|conn|auth` (case-insensitive), AND every `scheme://user:pass@host` URL — regardless of how the value looks. Credentials hide inside URLs and in innocuous-looking key-named fields.
- Prefer printing key names only.
- If a secret does leak into a transcript, commit, or log: flag it prominently and recommend rotating the exposed credential immediately.

<!-- nysa-agents:repo-standard:start -->
## Repository baseline (managed)

- Verification: run `bash ci/test-all.sh --changed-or-defer origin/main HEAD` plus `scripts/repo-check` and `scripts/secret-scan` before declaring a code change complete. When enabled, remote full CI records broad verification as deferred rather than passed.
- The protected default branch is `main`. Create short-lived branches matching `^(feat|fix|docs|chore|refactor|test|hotfix|spike)/[a-z0-9]+(?:-[a-z0-9]+)*$`; never push or merge without explicit approval.
- Never print credentials or raw secret-bearing configuration. Redact values by key name and credential-bearing URL before sharing output.
- Put disposable agent scratch and generated reports in gitignored `.context/`.
- Keep tracked cross-session truth in `context/memory.md` under `Current truth` and `Log`; promote stable knowledge instead of keeping raw transcripts.
- Stable documentation belongs in the declared documentation roots: `docs/`. Update the relevant document when its truth changes.
- Startup-critical rules belong in `AGENTS.md`; narrower subtree differences belong in scoped instruction files.
- Scoped instruction files: none.
<!-- nysa-agents:repo-standard:end -->
