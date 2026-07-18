# Linear workflow

Linear is the factory's visual and operator-facing board. Git remains the
durable execution record: ticket contracts, machine-readable verdicts, logs,
evidence bundles, and the cost ledger live under `factory/`. The reconciler
polls Linear and records operator-owned fields in the ignored
`factory/linear-map.json` overlay before projecting factory output back to
Linear. It never rewrites tracked ticket or initiative files. `preflight.sh`
and `next-stage.sh` never call a network API.

One shared **Software Factory** team contains every product. Each factory
initiative is one Linear Project; each `factory/tickets/T-NNN.md` is one issue.

## Field ownership

Linear is authoritative only for:

- issue priority and initiative/Project membership;
- Backlog → Ready;
- Awaiting Approval → Approved;
- Blocked-Escalated → a valid resumed stage.

The factory is authoritative for title, description, acceptance criteria,
frozen contract, branch/PR facts, role-stage movement, escalation into
Blocked-Escalated, evidence, cost, and Approved → Done after merge/deploy
confirmation. Unsupported Linear edits are restored from the ticket file.
Removing an issue from every Linear Project explicitly clears its effective
initiative; it cannot enter execution again until the operator assigns one.

Preflight, sequencing, and projection combine the overlay with the exact ticket
worktree or committed ticket branch. The launcher-managed `ticket-state`
command materializes accepted non-sensitive operator fields and commits ordinary factory-owned stage
moves on the ticket branch. Contract 1.2 stops in Review: transition and
materialization both refuse Awaiting Approval, Approved, and Done until trusted
bundle and merge/deploy attestation paths exist, and sequencing does not
authorize `AWAIT-MERGE`. Contract 1.3 keeps those generic refusals and adds
`ticket-attest`: bundle creates Awaiting Approval, approval consumes only the
exact newer Linear approval and enables protected auto-merge, and done records
verified merge/deployment closeout and requests protected auto-merge for its
factory-owned metadata/accounting PR. No second operator approval exists.
After that PR merges, protected-main terminal evidence yields `COMPLETE`,
releases the lease, and supplies Done to Linear sync. An API outage never stops an in-flight ticket. The local sync map and logs show
stale health, and new operator actions wait for the next successful pull. A
ticket already ingested as Ready continues from the local record.

## Workflow states

The columns deliberately match the factory's role sequence so an operator can
see the active stage without opening an issue.

| State | Meaning | Who moves it here |
|---|---|---|
| Backlog | Exists, not prioritized | anyone |
| Ready | Operator prioritized it; planner may pick it up | operator only |
| Planning | Planner and spec-linter are freezing and checking the contract | dispatcher |
| Building | Test author and builder are producing tests and implementation | dispatcher |
| Review | Reviewer and Narrator are checking the change and producing evidence | dispatcher |
| Awaiting Approval | Bundle is posted and needs an operator decision | dispatcher |
| Approved | Operator approved the bundle; merge/deploy is pending | operator only |
| Blocked-Escalated | Needs the operator: deadlock, contract problem, budget stop, open question | agents or operator |
| Done | Approved PR merged and staging deploy confirmed | factory close-out |

The operator's daily scan is two lists: **Blocked-Escalated** (decide something)
and **Awaiting Approval** (approve or send back). Everything else is agent
territory.

Legal happy-path transitions are:

`Backlog → Ready → Planning → Building → Review → Awaiting Approval → Approved → Done`.

This lifecycle is not permission for the generic transition API. Contract 1.2
stops at Review. Contract 1.3 performs the final three evidence-sensitive moves
only through the trusted attestation route.

Spec-lint failure stays in Planning. Review changes return to Building. A
broken preview returns to Building. Any active stage may enter
Blocked-Escalated; an operator may resume it at the stage named by
`Resume-State:` in the ticket.

The team intentionally archives Linear's default `Todo` and `In Progress`
states plus the former Spec Lint, Test Authoring, and Evidence micro-states.
Their detailed progress remains visible in the issue checklist and log without
making the board harder to scan.

## Initiatives and Projects

Each product repo stores initiatives in `factory/initiatives/I-NNN.md`:

```text
# Initiative name

Status: planned
Target-Date: 2026-09-30
View: factory

## Summary
The measurable outcome this initiative delivers.
```

Tickets contain `Initiative: I-NNN`. The reconciler creates the corresponding
Linear Project, stores its UUID in `linear-map.json`, and assigns every issue.
`View: factory` also creates a shared Project-filtered Factory Pipeline view
and stores its UUID with the initiative mapping.
Project status, target date, and issue membership may be edited in Linear and
are ingested into the operator overlay; the initiative summary remains Git-owned.

## Ticket template

```
# T-NNN — <one sentence>

State: Backlog
Initiative: I-NNN
Priority: none
Risk class: low | medium | high
External: no

## Description
<what and why, with source links>

## Acceptance criteria
1. <mechanically or visually checkable outcome>

## Factory checklist
- [ ] Contract frozen
- [ ] Spec lint passed
- [ ] Tests authored
- [ ] Implementation green
- [ ] Reviewer approved
- [ ] Evidence bundle posted
- [ ] Operator approved
- [ ] PR merged and staging confirmed

## Links
- Branch:
- PR:
- Evidence:

## Log
```

## Conventions

- One ticket = one builder session = one branch = one PR. The planner splits anything bigger.
- Bug tickets link back to the Done ticket that shipped the defect (this is the escaped-defect metric; see `docs/metrics.md`).
- Cost and attempts are posted on the ticket by the Narrator from the ledger.
- Apply `risk:low`, `risk:medium`, or `risk:high`; apply `external` when the change can send outside the system.
- Factory-managed sections in Linear carry a read-only notice. Operator notes belong in comments; structured approvals and resume decisions are ingested by state transition.

## Reconciler operations

Run `scripts/linear-sync.py --factory-root <product-repo> --setup` once, then
schedule `scripts/launchd/com.factory.linear-sync.plist.template` every three
minutes. `--dry-run` performs reads and prints both pull and push actions
without changing Linear or local files. Sync health is recorded under
`_sync` in `factory/linear-map.json`; investigate a stale timestamp before
trusting the board.
