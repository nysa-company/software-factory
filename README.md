# Software Factory Kit

A product-agnostic kit for running an AI software factory: agents plan, build, review, and document work in Git; a human sets priorities and approves from evidence bundles, never from diffs, through one-use operator receipts.

Built July 2026 for the Nysa project, factored out so any product can use it. Design decisions and their history live in the [Nysa product repository](https://github.com/nysa-company/nysa/blob/main/deliverables/2026-07-11-autonomous-software-factory-brief.md).

## What's in the box

| Folder | Contents |
|---|---|
| `docs/` | Product brief, architecture, factory setup, workflows, runbooks, operations, and metrics |
| `envelope/` | Budget and escalation template — the factory's hard limits |
| `roles/` | Versioned contracts for dispatcher, planner, spec-linter, test-author, builder, reviewer, and narrator |
| `scripts/` | Run wrapper with cost ledger, CLI adapters, kill switch, spend rollup |
| `ci/` | Executable regression checks and the product CI template |
| `conformance/` | The Nysa-shaped conformance product — the kit's permanent test bed |

## Worker model portfolios

There is no single global primary/secondary chain; each role has one. With no
operator activation, `cursor-opus-v1` is the default:

| Role | Primary | Secondary |
|---|---|---|
| Planner | Cursor GPT-5.6 Sol High, high effort | Codex GPT-5.6 Sol, high effort |
| Builder; Narrator | Cursor GPT-5.6 Sol High, high effort | Codex GPT-5.6 Terra, high effort |
| Spec-linter; Test-author | Cursor Claude Opus 5 Thinking, medium effort | Claude Fable 5, medium effort |
| Reviewer | Cursor Claude Sonnet 5 Thinking High, high effort | Claude Sonnet 5, high effort |

Cursor is a separate route but not a separate model family: Cursor GPT remains
OpenAI and Cursor Claude remains Anthropic. Before a ticket starts,
`UNAVAILABLE` advances to the next candidate; `INVALID` or `UNKNOWN` stops. The
complete six-role plan is pinned to the ticket branch.

Contract 1.4 and newer add operator-approved mid-ticket fallback for a terminal,
accounted provider or credit failure. It excludes the exact failed route,
preserves only validated role-authorized work, re-resolves all remaining roles
against contributor-family history, and appends an auditable route-journal
revision. The normal default transition is Cursor to its same-family native
Codex/Claude route. If no complete family-separated assignment exists,
the factory escalates instead of weakening review independence.

Kimi K2.6 remains disabled experimental and is in no profile. See
[Model routing and fallback](docs/model-routing.md) for the exact route order,
profile alternatives, family rules, approval flow, and operator commands.

## Local multi-project console

Contract 1.5 adds a loopback-only control console for every project registered
in the factory profile:

```bash
python3 scripts/operator-console.py
```

The printed one-use URL opens workflow, role/model/family/effort, envelope, and
daily spend views. Model policy dropdowns contain only catalog-authorized
routes and enforce distinct production/checking families. Envelope edits,
temporary role/ticket/day overrides, and active-attempt cancellation use
preview hashes: preview first, then explicitly apply the exact preview.

Changing an active attempt never rewrites its budget or prior accounting.
Cancel it, account it conservatively, approve the new envelope/model policy,
and restart at the same role boundary. A same-family Reviewer is never a
normal policy option; it requires an exact ticket-scoped, one-use operator
fallback-approval exception.

## Core rules (enforced by the kit, not by prompts)

1. Budgets live in the run wrapper and provider console caps. Agents cannot raise their own limits.
2. The builder cannot edit tests — CI fails the PR if builder commits touch test files.
3. Approval happens before merge, from a Narrator evidence bundle.
4. Test author and reviewer run on a different model family than the builder.
5. Two review rounds, then the ticket escalates to a human with a plain-language note.
6. One role attempt submits its task to at most one agent process. Any
   mid-ticket route change requires the Contract 1.4+ journal and one-use
   operator approval flow.

## How operator receipts and Markdown work together

Git is the durable execution record, and now the only one — there is no
external board. Every operator decision (ready, approve, resume, cancel,
priority, and model-fallback approval) is a one-use receipt issued by
`factory-kit.sh operator <action> --project SLUG --product REPO [--ticket
T-NNN]`. The receipt is anchored in the controller's state directory, is
projected into the gitignored `factory/operator-map.json` that ticket
sequencing reads, and gets a zero-authority audit copy under
`factory/receipts/<T>/` committed in the product checkout. The map is a
projection with nothing behind it, so there is no sync delay or staleness to
wait on.

The operator owns the decisions a human should make:

- ticket priority and initiative;
- Backlog → Ready;
- Awaiting Approval → Approved;
- the agreed resume stage for a Blocked-Escalated ticket;
- initiative status and target date, recorded directly in
  `factory/initiatives/I-NNN.md` — there is no external Project object to
  keep in sync.

Markdown owns acceptance criteria, the frozen contract, factory-stage
movement, machine-readable verdicts, logs, evidence bundles, costs, and final
Done close-out. Editing factory-owned text or moving a stage by hand instead
of through a receipt is rejected on the next reconciliation.

The compact board flow is:

`Backlog → Ready → Planning → Building → Review → Awaiting Approval → Approved → Done`

- Planning contains planner and spec-linter work.
- Building contains test-author and builder work.
- Review contains reviewer and Narrator/evidence work.
- Blocked-Escalated is reachable from any active phase.
- Approved means the operator authorized the change; Done means merge and
  staging deployment were also confirmed.

Detailed role progress remains in the issue checklist and log instead of
creating a column for every agent.

## Where the operator participates

The factory needs you at four points:

1. **Choose the work.** Ensure the durable initiative and ticket files exist.
   There is no external board to create or map anything in — a ticket file
   under `factory/tickets/` is a runnable ticket by itself.
2. **Prioritize it.** Run `operator priority`, then `operator ready`. Both
   take effect as soon as the receipt is issued; there is no sync-health
   timestamp to wait on.
3. **Resolve exceptions.** Read Blocked-Escalated notes, decide the requested
   question, set `Resume-State:` in the ticket record, and run
   `operator resume --stage <Resume-State>`.
4. **Approve the result.** In Awaiting Approval, read the evidence bundle and
   open the preview. Run `operator approve` only when it is safe to merge, or
   send it back with a concrete reason instead. Done is recorded after merge
   and staging confirmation.

Contract 1.2 still stops in Review. Contracts 1.3 through 1.5 implement the fourth step
through the trusted `ticket-attest` command: it attests the exact bundle,
consumes the one operator receipt-bound approval, requests protected
auto-merge, and records Done only after merge-commit deployment checks and
closeout accounting pass.
It also auto-merges the protected factory-owned closeout PR and releases any
dispatcher lease only after attested Done reaches main, so the normal operator
actions remain Ready and Approved.

## Does moving a ticket to Ready start the factory?

**Not by itself.** Running `operator ready` issues the receipt and updates the
operator map immediately — there is no external system or reconciliation
delay — but nothing autonomously dispatches from that alone.

After the Ready transition is visible locally, a dispatcher session must:

1. resolve the active contract with
   `~/.factory/bin/factory-launch <project> contract --json`;
2. create the exact clean ticket branch/worktree from current protected
   `origin/main`, then run `ticket-state --action materialize` to create and
   verify its remote ref;
3. run the launcher's `preflight` and `next-stage` routes with the exact ticket
   worktree required by that contract;
4. launch each role only through
   `~/.factory/bin/factory-launch <project> run`.

If a standing dispatcher is added later, Ready can become its kickoff signal.
Until then, Ready means **authorized and eligible to start**, not **already
running**. Never bypass a preflight failure.

## Quick start

Read the [product brief](docs/product-brief.md), [architecture](docs/architecture.md), and [factory setup checklist](docs/factory-setup.md). Copy the required templates into the product repo, fill the blanks, run the validator, then start with the [walking skeleton](docs/operations/walking-skeleton.md). Do not create a ticket backlog before its staging URL works.

For the complete transition and ownership rules, see
[`docs/workflows/ticket-flow.md`](docs/workflows/ticket-flow.md) and
[`docs/runbooks/operator.md`](docs/runbooks/operator.md).
