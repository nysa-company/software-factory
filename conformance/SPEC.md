# Conformance product — "Relay"

A deliberately small product shaped like Nysa's hard parts, used to shake down the factory kit and re-run after any kit change. It is disposable as a product and permanent as a test bed.

## Why these features

Nysa's real failure modes are not CRUD — they are intake, async processing, approval-before-external-action, and partial failure. Relay has exactly those and nothing else:

| Relay feature | Stands in for (Nysa) |
|---|---|
| Webhook event intake, idempotent by event id | Meeting-bot / email intake (duplicate deliveries happen) |
| Durable background job with retries + dead-letter | Transcript processing, knowledge extraction |
| Proposed action requiring approval before execution | Propose→approval autonomy rule |
| Sandboxed send (allowlisted recipients, outbox record) | Gmail send connector in sandbox mode |
| Crash recovery from persisted state | The always-on service being redeployed/killed mid-work |

## Behavior spec

1. `POST /webhook/event` with `{id, type, payload}` accepts an event. A repeated `id` is acknowledged but never creates a second job (idempotency).
2. Each accepted event enqueues one background job. The worker retries a failing job up to 3 attempts, then marks it dead (dead-letter) — visible, never silently dropped.
3. A successful job produces a **proposed action** (a send: `to`, `subject`, `body`) in state `pending`. Nothing is sent at this point.
4. `POST /api/approvals/:id/approve` executes the send **only if** the recipient is on the allowlist; the send is a record in the outbox (sandbox — no real delivery). A non-allowlisted recipient is refused even when approved, recorded as `blocked_recipient`.
5. `POST /api/approvals/:id/reject` closes the proposal; nothing sends.
6. All state persists to disk atomically on every change. Killing the process at any moment loses nothing: on restart, accepted events, unfinished jobs, and pending approvals resume.
7. `GET /` shows a minimal UI: events, jobs (with attempts), approvals, outbox. `GET /api/state` returns the same as JSON.

## Test cases (the conformance suite)

1. Walking skeleton: event in → visible in list.
2. Duplicate event id → exactly one job.
3. Transient failure (`payload.failTimes: 2`) → succeeds on attempt 3, attempts recorded.
4. Permanent failure → dead after 3 attempts, visible in dead-letter.
5. Approval gate: outbox empty before approval; send appears only after approve.
6. Allowlist: approved send to an unlisted recipient is blocked, recorded, not sent.
7. Crash recovery: kill the server mid-flight; restart; pending work completes; nothing duplicated.

Config via env: `PORT` (default 4700), `DATA_DIR` (default `./data`), `ALLOWLIST` (comma-separated, default `test@example.com`), `WORKER_MS` (worker tick, default 200).

## Known modeling limitation

Relay's "send" is an in-process outbox write persisted atomically with the status change, so the real-world failure window — crash *between* an external side effect and recording its receipt — cannot occur here. That window is a product-engine concern (idempotency keys on the connector, write-then-verify), owed by each product's engine spec; Relay tests everything up to that boundary.
