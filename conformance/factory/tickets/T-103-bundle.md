# T-103 Evidence Bundle — rejections carry a reason

## 1. What this does

When an operator rejects a proposed action, they can now say *why*. `POST /api/approvals/:id/reject` accepts an optional `{ "reason": "..." }` body; that reason is saved with the approval and shows up on it in `/api/state`. If no reason (or a garbage one) is given, the field is still present but `null` — never missing. Once an approval is rejected, its reason is locked: a second reject attempt is refused (`409`) and changes nothing, and the reason survives a hard server crash and restart.

## 2. Preview link

No preview deploy exists for this project (same gap as T-101 and T-102 — Railway PR-preview environments aren't wired up for this factory-trial app). What to try once one exists: create an event, then `POST /api/approvals/<id>/reject` with and without a `reason` body, and confirm `GET /api/state` shows the right value each time.

In place of a preview, all evidence below was captured against the real app running locally from this branch (`PORT=<port> DATA_DIR=$(mktemp -d) node server.js`), independently re-run for this bundle (not just copied from earlier role logs).

## 3. Demo evidence (real captured responses, this run)

**AC1 — reject with a reason:**
```
POST /api/approvals/appr-demo-ac1/reject   { "reason": "no longer needed" }
→ 200 {"ok":true,"status":"rejected","reason":"no longer needed"}

GET /api/state → matching approval:
{ "id": "appr-demo-ac1", "jobId": "job-demo-ac1", "action": {...}, "status": "rejected",
  "proposedAt": "2026-07-13T04:42:44.349Z", "reason": "no longer needed" }
```

**AC2 — reject with no body:**
```
POST /api/approvals/appr-demo-ac2/reject   (no body)
→ 200 {"ok":true,"status":"rejected","reason":null}

GET /api/state → matching approval has "reason": null
```

**AC3 — reject an already-rejected approval:**
```
POST /api/approvals/appr-demo-ac1/reject   { "reason": "trying to overwrite" }
→ 409 {"ok":false,"error":"already rejected"}

GET /api/state → appr-demo-ac1 unchanged: still "reason": "no longer needed"
```
Same call against an unknown id:
```
POST /api/approvals/appr-does-not-exist/reject
→ 404 {"ok":false,"error":"no such approval"}
```

**AC4 — reason survives SIGKILL + restart:**
```
POST /api/approvals/appr-demo-ac4/reject   { "reason": "persist after SIGKILL" }
→ 200 {"ok":true,"status":"rejected","reason":"persist after SIGKILL"}

[server killed with SIGKILL, restarted against the same DATA_DIR]

GET /api/state → appr-demo-ac4 still present with "reason": "persist after SIGKILL"
```

All four responses match the frozen contract in `T-103.md` §"Frozen contract" exactly, field-for-field.

## 4. Acceptance criteria

| # | Criterion | How verified | Result |
|---|-----------|--------------|--------|
| 1 | Reject with a reason → approval shows `rejected` with that exact reason | Test `1. Reject with a reason...` in `reject-reason.test.js` + live demo above | PASS |
| 2 | Reject without/invalid reason → approval shows `rejected` with `reason: null` | Test `2. Reject without a body...` in `reject-reason.test.js` (covers missing body, `{}`, `null`, number, object, array, boolean) + live demo above | PASS |
| 3 | Reject on non-pending → frozen `409`, record unchanged (incl. original reason) | Test `3. Rejecting an already-rejected...` in `reject-reason.test.js` (covers `rejected`, `sent`, `blocked_recipient`) + live demo above | PASS |
| 4 | Reason persists across `SIGKILL` + restart | Test `4. Reason survives a server kill and restart` in `reject-reason.test.js` + live demo above | PASS |
| — | Unknown `:id` → frozen `404` body (added per reviewer round 1) | Test `Unknown approval → 404...` in `reject-reason.test.js` + live demo above | PASS |

## 5. Test evidence — independently rerun for this bundle

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
✔ 1. Reject with a reason → the approval in /api/state is rejected and carries that exact reason string
✔ 2. Reject without a body or without a reason → the approval is rejected with the frozen no-reason representation
✔ 3. Rejecting an already-rejected (or otherwise non-pending) approval → 409 and the record is unchanged, including its original reason
✔ 4. Reason survives a server kill and restart
✔ Unknown approval → 404 with exactly the frozen no-such-approval response

tests 20, pass 20, fail 0
```
15 pre-existing tests + 5 new (4 acceptance + 1 reviewer-requested regression), all green. Reviewer verdict (round 2): APPROVE.

## 6. Review history

- **Round 1 — REQUEST CHANGES**: the frozen unknown-ID `404` contract was untested; the builder's serialization change (dropping the internal `code` field) could have shipped a wrong `404` body and still passed all 19 tests.
- **Fix**: test-author added an exact `404` status + body test for an unknown approval id (commit `257619e`).
- **Round 2 — APPROVE**: new test correctly catches the prior leaked-`code`-field risk; all four criteria adequately tested; implementation matches the frozen contract with no scope creep; original acceptance tests untouched by the builder; commit order correct; full suite independently rerun 20/20.

## 7. Commit list

| Commit | Description |
|---|---|
| `4634d32` | Freeze reject-reason contract (planner) |
| `75b85d3` | Escalate adapter version warning |
| `b9e45c7` | Record adapter escalation resolution |
| `a29b4a0` | Log test-author run |
| `672a598` | Add failing reject-reason acceptance tests (test-author, new `tests/reject-reason.test.js`, 4/4 failing as expected) |
| `f371048` | Cover all non-pending rejection states |
| `d58394d` | Log test-author verification |
| `a63b299` | Escalate daily budget refusal |
| `d53c748` | Record daily cap resolution |
| `c9893c7` | Merge `origin/main` into ticket branch |
| `f3d85bb` | Implement reject-reason contract (builder, `conformance/app/server.js`) |
| `7afaa3c` | Log builder run |
| `d9c51d8` | Record reviewer round 1 verdict (REQUEST CHANGES) |
| `257619e` | Cover unknown approval rejection — add frozen 404 test (test-author fix) |
| `23a4c88` | Log reviewer fix tests |
| `0662e55` | Record reviewer round 2 approval |

## 8. Risk

**Internal change — low risk**, per the evidence rubric. No `external` label: rejecting a proposal cannot trigger a send — the outbox is untouched by this ticket, and this only adds metadata (`reason`) to an existing record. No schema migration is involved; the `reason` field is additive and only appears on already-rejected approvals. Worst plausible failure is a wrong or leaked `reason` value on a rejected approval, which the acceptance tests guard against with exact `deepStrictEqual` checks on the full approval object, including the non-pending/immutability and restart-persistence paths.

## 9. Cost

Ledger rows for T-103 (`conformance/factory/ledger.csv`, main factory repo):

| Role | Round | Adapter | Cost | Notes |
|---|---|---|---|---|
| planner | 1 | claude-code | $1.0881249 | Froze the reject-reason contract; also absorbed a mid-run adapter-version escalation |
| test-author | 1 | codex | $0.4943 | New `tests/reject-reason.test.js`, expanded to cover all non-pending states, 4/4 failing as expected |
| builder | 1 | claude-code | $0.5681364 | Implemented the frozen contract in `server.js`, no test edits |
| reviewer | 1 | codex | $0.2206 | REQUEST CHANGES — add unknown-ID 404 test |
| test-author | 2 | codex | $0.2461 | Added the requested regression test; full suite 20/20 |
| reviewer | 2 | codex | $0.1490 | APPROVE |
| narrator | — | claude-code | $1.50 (reserved) | This bundle; reservation is a budget hold, not the final logged spend |
| **Completed role-run total** | | | **$2.7662613** | Sum of the six completed rows above (excludes the narrator reservation) |

Attempts: planner 1, test-author 2 rounds, builder 1 (no retries), reviewer 2 rounds. Two additional dispatcher-level escalations (installed-vs-pinned Claude Code version warning; daily budget cap refusal at $8.97 spent) were resolved by the operator without spawning a new role run, so they carry no ledger cost of their own.

## 10. Rollback

Fully reversible, no data or external effects to undo. Revert the implementation and test commits — `f3d85bb` (server.js) and `672a598`/`f371048`/`257619e` (the new `tests/reject-reason.test.js` and its extensions) — to restore the pre-T-103 behavior where reject took no reason. Once this branch is merged via a pull request, "revert PR #N" will be the single-step equivalent.

---

**Decision needed:** approve to merge, or send back with what's wrong.
