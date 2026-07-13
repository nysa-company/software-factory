# Linear setup

**Pilot-stage amendment (2026-07-13):** during the pilot, the ticket files in `factory/tickets/` are execution truth and Linear is a **one-way, read-only projection** maintained by `scripts/linear-sync.py` (launchd job `com.nysa.linear-sync`, every 3 minutes). Manual edits in Linear are overwritten on the next sync cycle. The board-of-record model described below is deferred — it would put a network API in the sequencer's control path — and may never be adopted; revisit only if the operator wants to prioritize or approve from Linear.

Linear is the only board of record. Execution state may exist elsewhere later (a dispatcher's internal ledger), but intent, priority, and status truth live here.

## Workflow states

Five working states plus Done. No more. "Spec'd" and "test-ready" are checklist items on the ticket, not states.

| State | Meaning | Who moves it here |
|---|---|---|
| Backlog | Exists, not prioritized | anyone |
| Ready | Operator prioritized it; planner may pick it up | operator only |
| In progress | Planner/test-author/builder working | agents |
| Review | Reviewer + Narrator stage; evidence bundle pending or posted | agents |
| Blocked-Escalated | Needs the operator: deadlock, contract problem, budget stop, open question | agents or operator |
| Done | Operator approved the bundle; PR merged | operator approval → merge automation |

The operator's daily scan is two lists: **Blocked-Escalated** (decide something) and **Review** (approve or send back). Everything else is agent territory.

## Ticket template

```
## What
<one sentence>

## Why / source
<link to acceptance-spec or engine-spec section>

## Checklist
- [ ] Spec'd (planner: description + acceptance criteria + frozen contract posted)
- [ ] Tests written (test-author: failing tests are first commits on branch)
- [ ] Implementation green (builder: CI passing)
- [ ] Reviewed (reviewer: approved, ≤2 rounds)
- [ ] Evidence bundle posted (narrator, pre-merge)

## Labels
external — if this change can send anything outside the system
```

## Conventions

- One ticket = one builder session = one branch = one PR. The planner splits anything bigger.
- Bug tickets link back to the Done ticket that shipped the defect (this is the escaped-defect metric; see `metrics/schema.md`).
- Cost and attempts are posted on the ticket by the Narrator from the ledger.
