# Shakedown report — 2026-07-11

First run of the kit against the Relay conformance product. Everything below was executed and verified locally on this machine, except the items in "Needs the operator", which require accounts only Javier holds.

## What was proven

| Mechanism | Result |
|---|---|
| Conformance suite (7 cases: skeleton, duplicate event, retry, dead-letter, approval gate, allowlist block, SIGKILL crash recovery) | all green (`npm test`, 1.4s) |
| Walking skeleton live | event in → job processed → approval → sandboxed send in outbox → UI renders; verified via curl against a running server |
| Run wrapper ledger | runs logged with role, adapter, prompt version, turns, cost |
| Daily cap | third run refused at $2.32 / $2.00 cap, exit 5 |
| Kill switch | KILL file blocks new runs, exit 4; resume by removing the file |
| Spend rollup | correct daily total and per-role breakdown from the ledger |
| Test-immutability gate | passes on a clean builder branch; fails (exit 1) when a builder commit touches a test file |
| Rollback drill | bad change merged, reverted, suite green after revert |
| Adapter contract test | catches real CLI drift (see finding 1), passes after fix |

## Findings (kit changes made)

1. **Claude Code 2.1.207 dropped `--max-turns`** — the contract test caught it on first run, exactly as designed. Better news: the CLI now has `--max-budget-usd`, a hard in-run dollar stop, which is stronger enforcement than the turn cap. Adapter rewritten around it; contract test now asserts `--max-budget-usd` exists; versions pinned (Claude Code 2.1.207, Codex 0.144.1).
2. **Codex cost figures are estimates.** `codex exec --json` exposes token counts, not dollars; the adapter estimates from configurable per-token rates and logs a warning when tokens are missing. Reconcile against the OpenAI console during the first real pilot week.
3. **`node --test <dir>` misbehaved**; the test script pins the explicit file. Trivial, but exactly the class of scaffold bug the walking skeleton exists to catch.
4. **A mock adapter earns its place in the kit** (`scripts/adapters/mock.sh`) — every wrapper mechanic was verified for $0.
5. **Ticket branches must merge, not delete.** During the immutability demo the ticket branch was deleted unmerged and the shakedown's own adapter fixes were nearly lost (recovered via reflog). The ticket-flow doc's rule — one branch, one PR, merged via approval — exists for a reason; the drill violated it and paid.

## Needs the operator (not executable from this session)

- GitHub remote for this repo + branch protection + the CI template wired (`ci/github-actions-ci.template.yml`).
- Linear board per `docs/workflows/linear.md`, and `LINEAR_API_KEY` for the spend rollup.
- Railway account for staging/preview deploys (`docs/operations/railway.md`).
- Console spend caps on Anthropic + OpenAI, and the three named API keys.
- A first real-CLI ticket: one Relay ticket run end to end with actual `claude`/`codex` runs through `~/.factory/bin/factory-launch <project> run` (everything here used the mock adapter or direct execution; real-CLI runs spend real money and were left for the operator's go-ahead).

## Hardening round (same day) — two-model review of the built kit

Fresh Fable and GPT Sol sessions read every file and returned **not done**, with overlapping findings. All fixed and re-verified:

1. **Immutability gate bypass (both reviewers):** v1 exempted commits whose *message* contained `[test-author]` — a marker the builder itself controls. Rewritten to two identity-free mechanical rules: a commit may touch tests or code, never both (separation); and every test commit must precede every implementation commit (order). Verified: clean branch passes; late test edit fails rule 2; a mixed commit carrying the old marker fails rule 1.
2. **Kill switch gaps (both):** the KILL file was `$PWD`-relative (running from the wrong directory stopped nothing) and `pgrep -x claude` would kill the operator's own sessions. Now both scripts anchor to the repo root, and the switch targets only factory-launched runs via PID files the wrapper writes (`factory/runs/*.pid`). Verified from a subdirectory.
3. **Budget holes (both):** cap checks now *reserve* the new run's full per-run budget under a lock (no starting at $74.99 of a $75 cap; no concurrent-run race), a per-ticket budget across all runs is enforced (`PER_TICKET_BUDGET_USD`), and an unparsable run cost keeps the conservative full-budget reservation instead of silently logging $0. All three verified with the mock adapter.
4. **Cross-family rule became mechanical:** the wrapper now maps roles to adapters (builder/planner → claude-code; test-author/reviewer → codex) and refuses mismatches, instead of trusting prompts.
5. **Operator approval became mechanical:** branch protection now requires 1 approving GitHub review (the operator's, after reading the bundle) — previously 0, which made approval-before-merge procedural only.
6. Smaller fixes: bash-3.2-safe adapter invocation (macOS stock shell), stale `--max-turns` row in the envelope template, FACTORY.md now copies all workflow files and defines the `ENVELOPE.env` format, runbook "drop the KILL file" ambiguity, rejection-path conformance test added (suite now 8 green), crash-recovery test now asserts the job is provably mid-flight (attempted, not done) at kill time, and Relay's SPEC documents its known modeling limit (crash between an external side effect and its receipt is a product-engine concern, not testable in Relay).

## Verdict (post-hardening)

The enforcement layer now holds against its own reviewers' attacks: no identity-based bypasses, anchored kill switch, reservation-based caps, mechanical cross-family and approval rules. Conformance suite: 8/8 green. Remaining honest gap, unchanged: **no ticket has yet run with real (non-mock) CLI agents** — that first real run is what the operator go-ahead buys, and it is the kit's final acceptance test.
