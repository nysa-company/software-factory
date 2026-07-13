# T-104 — Evidence bundle: operator can retry a dead job

## 1. What this does

A job that fails 3 times gets stuck in the dead-letter queue forever, with no way to bring it back. This change adds a single button-press action — `POST /api/jobs/:id/retry` — that puts a dead job back in the queue with a fresh 3-attempt budget, so the worker tries it again on its normal cycle. The job's history is never lost: total attempts keep accumulating across retries, and every job now shows a `retries` count so you can see at a glance whether it was ever retried.

## 2. Preview

This app has no hosted preview deploy — it's a local, zero-dependency Node service run from a worktree (see `conformance/SPEC.md`). To try it yourself:

```
cd conformance/app
DATA_DIR=/tmp/t104-try PORT=4799 WORKER_MS=100 node server.js
```

**What to try:** POST an event with a payload that always fails (`{"id":"e1","type":"send_email","payload":{"to":"test@example.com","subject":"try","failTimes":3}}`) to `http://localhost:4799/webhook/event`, wait ~1s for it to go dead-letter, then `POST http://localhost:4799/api/jobs/job-e1/retry` and check `GET /api/state` — the job flips back to `pending`, then to `done`, with `retries: 1`.

## 3. Screenshots / live demo transcript

No UI changed (this is an API-only ticket); evidence below is the actual request/response transcript from a live local run, not test output.

**AC3 — retry refused on a non-dead job** (job `job-e-pending`, status `done`):
```
$ curl -s -X POST http://localhost:4799/api/jobs/job-e-pending/retry
HTTP/1.1 409 Conflict
{"ok":false,"error":"only dead jobs can be retried (status: done)"}
```

**Unknown job → 404** (frozen contract, not a numbered AC but exercised live):
```
$ curl -s -X POST http://localhost:4799/api/jobs/job-nope/retry
HTTP/1.1 404 Not Found
{"ok":false,"error":"no such job"}
```

**AC1 — dead job, cause fixed, retried → completes and produces its proposed action** (job `job-e-ac1`, `failTimes:3`):
```
before retry: {"status":"dead","attempts":3,"lastError":"simulated failure on attempt 3","retries":0,"attemptsSinceRetry":3}
retry response: {"ok":true,"status":"pending","retries":1}
after next tick: {"status":"done","attempts":4,"lastError":null,"retries":1,"attemptsSinceRetry":1}
approval created: {"id":"appr-e-ac1","action":{"to":"test@example.com","subject":"ac1-demo","body":"Proposed by Relay worker."},"status":"pending"}
```

**AC2 — dead job, retried, still failing → dead again after exactly 3 new attempts, cumulative attempts visible** (job `job-e-demo`, `failTimes:99`):
```
before retry: {"status":"dead","attempts":3,"retries":0,"attemptsSinceRetry":3}
after retry + 3 fresh attempts: {"status":"dead","attempts":6,"lastError":"simulated failure on attempt 6","retries":1,"attemptsSinceRetry":3}
```

**AC4 — kill right after retry accepted; restart processes the re-queued job** (job `job-e-ac4`):
```
retry response: {"ok":true,"status":"pending","retries":1}
$ kill -9 <server pid>
state.json on disk immediately post-kill: {"status":"pending","attempts":3,"retries":1,"attemptsSinceRetry":0}
--- server restarted, same DATA_DIR ---
state after restart tick: {"status":"done","attempts":4,"lastError":null,"retries":1,"attemptsSinceRetry":1}
approval created: {"id":"appr-e-ac4","status":"pending", ...}
```

## 4. Acceptance criteria

| # | Criterion | Verified by | Result |
|---|---|---|---|
| AC1 | Dead job retried → pending again; cause-fixed job completes and produces its proposed action | `tests/retry-dead.test.js` test 1 + live demo (`job-e-ac1` above) | **PASS** |
| AC2 | Dead job retried, still failing → dead again after exactly 3 new attempts, cumulative attempts visible | `tests/retry-dead.test.js` test 2 + live demo (`job-e-demo` above) | **PASS** |
| AC3 | Retry of a non-dead job (pending or done) → refused with frozen status code, record unchanged | `tests/retry-dead.test.js` test 3 + live demo (`job-e-pending`, `409`) | **PASS** |
| AC4 | Kill server right after retry accepted; restart processes the re-queued job (crash recovery) | `tests/retry-dead.test.js` test 4 + live demo (SIGKILL + restart on `job-e-ac4`) | **PASS** |
| — | Unknown `:id` → frozen `404` response (contract item, not separately numbered) | `tests/retry-dead.test.js` "Unknown job → 404" + live demo | **PASS** |

**Targeted suite:** `node --test tests/retry-dead.test.js` → **5/5 pass**
**Full suite:** `node --test tests/*.test.js` → **25/25 pass** (all pre-existing tests unaffected, confirming the planner's exact-body audit held)

**Commit order / test-immutability gate:**
- `58823c9` (corrected acceptance tests) is the direct git parent of `780e5bb` (implementation) — verified with `git merge-base --is-ancestor 58823c9 780e5bb` (true).
- `ci/test-immutability-check.sh` run fresh against this branch: `test immutability holds: test commits are pure and all precede implementation (conformance/app/tests/)` — **exit 0**.

## 5. Risk

**Internal change.** Touches worker/dead-letter lifecycle and adds one new route; no external side effects are introduced by this ticket itself. The existing email-send path is unchanged and still gated behind the pre-existing approval flow — a retry can only ever get a job back to `done` with a *pending* approval, never send anything on its own.

What could go wrong: a bug in the reset logic (e.g. `attemptsSinceRetry` not resetting, or `attempts`/`lastError` being touched on retry) would let a job either dead-letter after fewer/more than 3 fresh attempts or misreport its history — this is exactly what the reviewer's round-1 controlled-worker assertions and the AC2 two-cycle test now guard against, both confirmed passing above.

## 6. Cost

Per the ticket log / ledger, cumulative spend through reviewer round 4 approval: **$3.7604721** across 8 logged role-runs — planner ×1, test-author ×2, builder ×1, reviewer ×4 (round 1, round 2, an overlapping duplicate round 2, round 4 approve; round 3 was escalated and never executed). This narrator/bundle stage is a 9th run and will be logged separately once its own ledger line is written.

**Attempts:** 2 reviewer rounds requested changes before approval (round 2's verdict also triggered an operator-authorized history reorder, and round 3 was blocked on sequencer authorization rather than re-reviewed) — round 4 is the first and only APPROVE.

## 7. Rollback

This branch (`ticket/T-104-retry-dead-job`) has not yet been merged to `main`. Once merged, reverting that merge commit restores the previous behavior exactly: the new route and the two additive job fields (`retries`, `attemptsSinceRetry`) disappear, and the dead-letter trigger reverts from `attemptsSinceRetry` back to cumulative `attempts`. No data migration is involved — `state.json` simply carries two extra keys per job that older code ignores, so a revert is safe even against data written post-merge.

---

**Approve to merge, or send back with what's wrong?**
