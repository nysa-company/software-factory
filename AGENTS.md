# Software Factory Kit

Product-agnostic kit for running an AI software factory. Read `FACTORY.md` before changing the engine model, role contracts, ticket flow, or product-instantiation contract.

## Run checks

- Full suite: `bash ci/test-all.sh`
- Baseline: `scripts/repo-check`
- Secret history and working tree: `scripts/secret-scan`
- Target the smallest relevant shell test while iterating, then run the full suite before completion.

## Scoped instructions

- `conformance/AGENTS.md` adds synthetic-data, Node command, and test-immutability constraints for the Relay proof app. Read it before changing anything under `conformance/`.

## Conventions

- Keep the kit product-agnostic; Nysa-specific state belongs in the Nysa product repository.
- Reuse the existing shell and Python helpers before adding dependencies.
- Preserve the builder/test-author separation and evidence-first approval flow.
- Update `FACTORY.md` and the relevant runbook when an engine contract changes.
- Future raw agent output is local-only under `.context/factory-runs/`; never commit it. The already tracked historical run outputs remain unchanged until the operator separately approves their removal.

## Session end

Append an entry to `context/memory.md` for significant decisions, reversals, incidents, or system changes. Keep its Current truth section synchronized.

## Learning promotion

- Keep one-off notes in `.context/`, cross-session project truth in `context/memory.md`, and stable repository rules in `AGENTS.md`, documentation, or deterministic checks.
- Propose a shared plugin skill only after the workflow repeats three times, appears across repositories, or is risky enough to require deterministic consistency.
- Use the plugin's `/skill-promote` workflow in a clone or worktree of `nysa-agents-plugin`; never edit an installed plugin cache or publish a skill without review.

## Git (team standard)

- Branch before committing if on the default branch — never commit directly to it.
- Ticket work uses `ticket/T-NNN-<slug>`; close-out ledger work uses `bookkeeping/T-NNN-closeout`; other maintenance uses `<type>/<short-kebab-summary>` as declared in `.agents/repo-standard.json`.
- On non-default branches, commit at logical checkpoints and at session end without asking. Never push, merge, or open a PR unless the user explicitly asks.
- Commit messages: a concise summary line that explains the *why*.
- Use the `gh` CLI for GitHub operations (PRs, issues).

## Never expose credentials (team standard)

- Never commit secrets. `.env` and key files stay gitignored; only `.env.example` is committed.
- When printing back any config, env vars, JSON, or logs, redact every value whose KEY matches `key|token|secret|password|url|dsn|conn|auth` (case-insensitive), AND every `scheme://user:pass@host` URL — regardless of how the value looks. Credentials hide inside URLs and in innocuous-looking key-named fields.
- Prefer printing key names only.
- If a secret does leak into a transcript, commit, or log: flag it prominently and recommend rotating the exposed credential immediately.
