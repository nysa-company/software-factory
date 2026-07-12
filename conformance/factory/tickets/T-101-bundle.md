# T-101 Evidence Bundle — /health reports queue depth

## 1. What this does

The app's health check page now also shows the state of the job queue: how many jobs are waiting, how many finished successfully, and how many failed for good. Before this change, the health check only said "the server is up" — now an operator can see at a glance whether work is flowing or piling up. Nothing else in the app changed.

## 2. Try it

No preview deploy exists for this project yet, so the demo below was run against the real app started locally from this branch (`PORT=4750 DATA_DIR=$(mktemp -d) node server.js`). What to try when a deploy exists: open `/health` in a browser — you should see `ok: true` and the three queue counts.

## 3. Demo evidence (real captured responses)

Fresh server, before any events:

```json
GET /health
{"ok":true,"queue":{"pending":0,"done":0,"dead":0}}
```

One event submitted:

```json
POST /webhook/event  {"id":"demo-1"}
{"ok":true,"duplicate":false,"eventId":"demo-1"}
```

Immediately after — the job shows as waiting:

```json
GET /health
{"ok":true,"queue":{"pending":1,"done":0,"dead":0}}
```

Two seconds later — the job finished and moved to done:

```json
GET /health
{"ok":true,"queue":{"pending":0,"done":1,"dead":0}}
```

## 4. Acceptance criteria

| # | Criterion | How verified | Result |
|---|-----------|--------------|--------|
| 1 | Fresh server: `ok: true`, all queue counts 0 | Test `1. on a fresh server, /health reports an empty queue` + live demo above | PASS |
| 2 | After an event completes, `queue.done` goes up by 1 | Tests `1b` and `2` + live demo above (pending 1 → done 1) | PASS |
| 3 | After a job exhausts retries (`failTimes: 99`), `queue.dead` goes up by 1 | Test `3. after a job exhausts retries, /health reports queue.dead incremented by 1` | PASS |

All gates green: health tests 4/4, full conformance suite 8/8, immutability gate green. Reviewer verdict (round 2): APPROVE.

## 5. Risk

**Low — internal change only.** Read-only endpoint: it reports numbers but doesn't send anything anywhere, change any data, or add new endpoints. Worst plausible failure is the health page showing a wrong count, which the tests guard against.

## 6. Cost

| Role | Spend |
|------|-------|
| test-author | $0.60 (two rounds: $0.35 + $0.25) |
| builder | $2.17 ($0.69 budget-stopped attempt + $1.48 successful) |
| reviewer | $1.08 (two rounds: $0.73 + $0.35) |
| narrator | $1.00 |
| **Ticket total** | **$4.86** (exact, from `factory/ledger.csv`) |

Attempts: builder 2 (first hit its budget and was stopped), reviewer 2 rounds (round 1 requested stronger tests; test-author round 2 addressed it).

## 7. Rollback

Reverting the single implementation commit `aa6adc9` restores the previous `/health` behavior. Fully reversible; no data or external effects to undo.

---

**Decision needed:** approve to merge, or send back with what's wrong.
