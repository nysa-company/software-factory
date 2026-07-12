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
- Linear board per `workflows/linear.md`, and `LINEAR_API_KEY` for the spend rollup.
- Railway account for staging/preview deploys (`ci/railway.md`).
- Console spend caps on Anthropic + OpenAI, and the three named API keys.
- A first real-CLI ticket: one Relay ticket run end to end with actual `claude`/`codex` runs via `run-agent.sh` (everything here used the mock adapter or direct execution; real-CLI runs spend real money and were left for the operator's go-ahead).

## Verdict

The kit's enforcement layer works mechanically end to end. The loop's paperwork (roles, flow, rubric) is written but has not yet been exercised with real model runs — that is the first thing the operator go-ahead should buy.
