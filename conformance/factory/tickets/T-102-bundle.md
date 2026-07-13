# T-102 Evidence Bundle — /health reports approvals summary

## 1. What this does

The app's health check now also shows the approvals pipeline, not just the job queue. Alongside the queue counts T-101 added, `/health` reports how many proposed actions are waiting for a human (`pending`), how many were sent, how many were rejected, and how many were blocked because the recipient wasn't on the allowlist. An operator can now see at a glance whether approvals are piling up, without opening the full UI. Nothing about the existing `ok` and `queue` fields changed.

## 2. Preview link

No preview deploy exists for this project yet (same gap noted in the T-101 bundle — Railway PR-preview environments aren't wired up for this factory-trial app). What to try once one exists: open `/health` in a browser and confirm the response includes an `approvals` object with four zeroed counts on a fresh deploy.

In place of a preview, the demo evidence below was captured against the real app running locally from this branch (`PORT=<port> DATA_DIR=$(mktemp -d) node server.js`).

## 3. Demo evidence (real captured responses)

**AC1 — fresh server:**
```json
GET /health
{"ok":true,"queue":{"pending":0,"done":0,"dead":0},"approvals":{"pending":0,"sent":0,"rejected":0,"blocked_recipient":0}}
```

**AC2 — after an event's job completes, one pending approval:**
```json
POST /webhook/event  {"id":"demo-t102-ac"}
{"ok":true,"duplicate":false,"eventId":"demo-t102-ac"}

GET /health   (after the worker processes the job)
{"ok":true,"queue":{"pending":0,"done":1,"dead":0},"approvals":{"pending":1,"sent":0,"rejected":0,"blocked_recipient":0}}
```

**AC3 — after that approval is approved (allowlisted recipient), it moves to sent and pending returns to zero:**
```json
POST /api/approvals/appr-demo-t102-ac/approve
{"ok":true,"status":"sent"}

GET /health
{"ok":true,"queue":{"pending":0,"done":1,"dead":0},"approvals":{"pending":0,"sent":1,"rejected":0,"blocked_recipient":0}}
```

All three responses match the frozen contract in `T-102.md` §"Frozen contract" exactly, field-for-field.

## 4. Acceptance criteria

| # | Criterion | How verified | Result |
|---|-----------|--------------|--------|
| 1 | Fresh server reports zero approvals in every status | Test `1. Fresh server` in `health-approvals.test.js` + live demo above | PASS |
| 2 | After a job completes, `/health` shows one pending approval | Test `2. One pending approval` in `health-approvals.test.js` + live demo above | PASS |
| 3 | After approval, it shows under `sent` and pending returns to zero | Test `3. Approved` in `health-approvals.test.js` + live demo above | PASS |

## 5. Test evidence — suite counts before and after

**Before (builder's first implementation attempt, commit `5e201e0`):** 11 passed, 4 failed. The 4 failures were all pre-existing T-101 assertions in `tests/health.test.js` (`assert.deepStrictEqual` against object literals with no `approvals` key — any additional top-level key fails them by construction). `health-approvals.test.js` (3/3) and `conformance.test.js` (8/8) were already fully green at this point; the builder correctly declined to touch test files and escalated instead of improvising a shape change.

**After (test-author fix, commit `81dd75e`, re-verified live in this run):**
```
node --test tests/*.js
✔ 1. walking skeleton: event in, visible in state
✔ 2. duplicate event id creates exactly one job
✔ 3. transient failure retries then succeeds, attempts recorded
✔ 4. permanent failure dead-letters after 3 attempts
✔ 5. approval gate: nothing sends before approve, sends after
✔ 6. allowlist: approved send to unlisted recipient is blocked, not sent
✔ 6b. reject: closes the proposal, nothing sends
✔ 7. crash recovery: SIGKILL mid-flight loses nothing, duplicates nothing
✔ 1. Fresh server
✔ 2. One pending approval
✔ 3. Approved
✔ 1. on a fresh server, /health reports an empty queue
✔ 1b. after an event is accepted, /health reports queue.pending incremented by 1
✔ 2. after a job completes, /health reports queue.done incremented by 1
✔ 3. after a job exhausts retries, /health reports queue.dead incremented by 1

tests 15, pass 15, fail 0
```
Full conformance suite: 15/15 green. Reviewer verdict (round 2): APPROVE.

## 6. Escalation and resolution

This ticket surfaced a genuine contract conflict, not a bug, and took three rounds of operator adjudication to close:

1. **Builder found the conflict, didn't paper over it.** Implementing the frozen `approvals` field on `/health` necessarily breaks the 4 pre-existing T-101 assertions in `tests/health.test.js`, which use `assert.deepStrictEqual` against object literals with no `approvals` key — no server-side shape satisfies both "exact frozen contract" and "those literals match exactly." Per the no-test-editing rule, the builder left `health.test.js` untouched, did not commit past the flag, and escalated (State → Blocked-Escalated) rather than guessing at a fix.
2. **Operator decision:** the frozen additive contract stands; test-author is authorized to update only the four stale exact-match expectations in `health.test.js` to include the `approvals` object.
3. **Dispatcher-level sequencer mismatch:** the mandatory `next-stage.sh --ticket T-102` returned `RUN reviewer` instead of `RUN test-author`, blocking dispatch a second time. Operator resolution: no state surgery needed — the stale pre-T-102 assertions are the fault, not the sequencer, and `RUN reviewer` was the legally correct next step (reviewer adjudicates the conflict before any test edit).
4. **Reviewer round 1: REQUEST CHANGES** — asked for exactly the fix already authorized: update the four stale `/health` expected bodies in `health.test.js` to include the frozen `approvals` object with scenario-appropriate counts.
5. **Dispatcher-level bookkeeping mismatch (second):** the sequencer refused dispatch because it couldn't find the round-1 reviewer verdict on `main` (it existed on the ticket branch, `a2aba87`). Operator resolution: during T-102 runs, ticket bookkeeping is read from the ticket worktree while the ledger stays in the factory repo — use `FACTORY_ROOT=~/Projects/sf-worktrees/T-102/conformance` with `FACTORY_LEDGER=~/Projects/software-factory/conformance/factory/ledger.csv`.
6. **Test-author fix** (commit `81dd75e`): changed only the four stale expectations; full suite green 15/15.
7. **Reviewer round 2: APPROVE** — round 1 addressed exactly, commit order and test-immutability discipline hold, implementation matches the frozen contract, full suite passes.

Net effect: the frozen contract in `T-102.md` was never changed — only the stale pre-T-102 test expectations were brought into line with it. No product-code shape was altered to work around the escalation.

## 7. Commit list

| Commit | Description |
|---|---|
| `8f0bc25` | Freeze `GET /health` `approvals` contract (planner) |
| `5ed8491` | Log T-102 planner completion |
| `18a7731` | test: pin health approvals summary contract (test-author, new `health-approvals.test.js`, 3/3 failing as expected pre-implementation) |
| `2ded4d0` | Log T-102 test-author completion |
| `5e201e0` | Implement `approvals` summary on `GET /health`; flag T-101 test conflict (builder, `conformance/app/server.js`) |
| `df1b829` | Escalate T-102 test contract conflict |
| `858a190` | Record T-102 operator contract decision |
| `4da596f` | Escalate T-102 sequencer mismatch |
| `1886131` | Record T-102 reviewer adjudication path |
| `a2aba87` | Record T-102 reviewer round 1 verdict (REQUEST CHANGES) |
| `5f80833` | Escalate T-102 sequencer verdict refusal |
| `3937ca6` | Record T-102 worktree sequencer resolution |
| `81dd75e` | Update stale health expectations for approvals summary (test-author fix, `conformance/app/tests/health.test.js`) |
| `1221d86` | Log T-102 test-author fix completion |
| `4c94e59` | Record T-102 reviewer round 2 approval (APPROVE) |

## 8. Risk

**Internal change — low risk**, per the evidence rubric: read-only endpoint, no schema change, no external side effects. `/health` only counts existing in-memory state; it does not send anything, write to a datastore schema, or expose a new mutating endpoint. Worst plausible failure is the reported counts being wrong, which the 3 new contract tests and the corrected T-101 suite both guard against with exact-match assertions.

## 9. Cost

Ledger rows for T-102 (`conformance/factory/ledger.csv`):

| Role | Round | Adapter | Cost | Notes |
|---|---|---|---|---|
| planner | 1 | claude-code | $0.3306252 | Froze the `approvals` contract |
| test-author | 1 | codex | $0.2296 | New `health-approvals.test.js`, 3/3 failing as expected |
| builder | 1 | claude-code | $0.6861495 | Implemented contract; found and correctly escalated the T-101 test conflict instead of committing past it |
| reviewer | 1 | codex | $0.1776 | REQUEST CHANGES — fix the 4 stale `health.test.js` expectations |
| test-author | 2 | codex | $0.1672 | Applied the authorized fix; full suite green 15/15 |
| reviewer | 2 | codex | $0.1258 | APPROVE |
| narrator | — | claude-code | $1.50 (reserved) | This bundle; reservation is a budget hold, not the final logged spend |
| **Completed role-run total** | | | **$1.7169747** | Sum of the six completed rows above (excludes the narrator reservation) |

Attempts: planner 1, test-author 2 rounds, builder 1 (escalated rather than retried — the blocker was a genuine cross-ticket contract conflict, not a failed attempt), reviewer 2 rounds. Three additional dispatcher-level sequencer/bookkeeping escalations were resolved by the operator without spawning a new role run, so they carry no ledger cost.

## 10. Rollback

Fully reversible, no data or external effects to undo. Revert the implementation and its accompanying test commits — `5e201e0` (server.js), `18a7731` (new approvals test file), and `81dd75e` (the corrected T-101 expectations) — to restore the pre-T-102 `/health` behavior (`ok` + `queue` only). Once this branch is merged via a pull request, "revert PR #N" will be the single-step equivalent.

---

**Decision needed:** approve to merge, or send back with what's wrong.
