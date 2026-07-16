# Software Factory Kit

A product-agnostic kit for running an AI software factory: agents plan, build, review, and document work on a Linear board; a human sets priorities and approves from evidence bundles, never from diffs.

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

## Role backends

| Role | Group | Primary provider / adapter | Primary model | Optional fallback / model |
|---|---|---|---|---|
| Dispatcher | Coordinator | Hermes Agent (deployment-configured provider) | Deployment-configured; outside the worker backend policy | None |
| Planner | Production | OpenAI / Codex CLI | CLI default (`cli-default` in the ledger; not pinned by the kit) | Cursor Agent / `gpt-5.6-sol-high` |
| Spec-linter | Checking | Anthropic / Claude Code CLI | CLI default (`cli-default` in the ledger; not pinned by the kit) | Cursor Agent / `claude-sonnet-5-thinking-high` |
| Test-author | Checking | Anthropic / Claude Code CLI | CLI default (`cli-default` in the ledger; not pinned by the kit) | Cursor Agent / `claude-sonnet-5-thinking-high` |
| Builder | Production | OpenAI / Codex CLI | CLI default (`cli-default` in the ledger; not pinned by the kit) | Cursor Agent / `gpt-5.6-sol-high` |
| Reviewer | Checking | Anthropic / Claude Code CLI | CLI default (`cli-default` in the ledger; not pinned by the kit) | Cursor Agent / `claude-sonnet-5-thinking-high` |
| Narrator | Production | OpenAI / Codex CLI | CLI default (`cli-default` in the ledger; not pinned by the kit) | Cursor Agent / `gpt-5.6-sol-high` |

Cursor fallback is disabled by default. When enabled, the wrapper selects the
matching provider-family route before submitting the task; fallback is never a
retry after a failed run.

## Core rules (enforced by the kit, not by prompts)

1. Budgets live in the run wrapper and provider console caps. Agents cannot raise their own limits.
2. The builder cannot edit tests — CI fails the PR if builder commits touch test files.
3. Approval happens before merge, from a Narrator evidence bundle.
4. Test author and reviewer run on a different model family than the builder.
5. Two review rounds, then the ticket escalates to a human with a plain-language note.
6. Backend fallback is pre-execution selection, never retry: one role run submits its task to at most one agent process.

## How Linear and Markdown work together

Linear is the visual board and the operator's decision surface. Git is the
durable execution record. Every `factory/initiatives/I-NNN.md` file maps to a
Linear Project, and every `factory/tickets/T-NNN.md` file maps to a Linear
issue. A per-product `com.factory.linear-sync.*` job reconciles them every
three minutes.

Linear owns the decisions a human should make:

- issue priority and initiative/Project membership;
- Backlog → Ready;
- Awaiting Approval → Approved;
- the agreed resume stage for a Blocked-Escalated ticket;
- Project status and target date.

Markdown owns acceptance criteria, the frozen contract, factory-stage
movement, machine-readable verdicts, logs, evidence bundles, costs, and final
Done close-out. Editing factory-owned text or moving an issue through agent
stages directly in Linear is corrected on the next sync.

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
   The reconciler creates and maps their Linear Project and issue. Creating an
   unrelated issue only in Linear does not create a runnable factory ticket.
2. **Prioritize it.** Set the Linear priority and Project, then move Backlog →
   Ready. Wait for the sync-health timestamp in `factory/linear-map.json` to
   advance.
3. **Resolve exceptions.** Read Blocked-Escalated notes, decide the requested
   question, set `Resume-State:` in the ticket record, and move the Linear
   issue to that agreed phase.
4. **Approve the result.** In Awaiting Approval, read the evidence bundle and
   open the preview. Move the issue to Approved only when it is safe to merge,
   or send it back with a concrete reason. Done is recorded after merge and
   staging confirmation.

That fourth step describes the target lifecycle. Contract 1.2 currently stops
in Review after the Narrator posts the bundle: its generic ticket-state command
cannot move a ticket to Awaiting Approval or Done. Wait for the dedicated
bundle and merge/staging attestation paths before using the approval close-out.

## Does moving a ticket to Ready start the factory?

**Not by itself in the current installation.** Moving a mapped Linear issue to
Ready is reconciled into its Markdown ticket within about three minutes, but
the installed job is a reconciler, not an autonomous dispatcher.

After the Ready transition is visible locally, a dispatcher session must:

1. run `scripts/preflight.sh --ticket T-NNN`;
2. follow `scripts/next-stage.sh --ticket T-NNN`;
3. launch each role through `scripts/run-agent.sh`.

If a standing dispatcher is added later, Ready can become its kickoff signal.
Until then, Ready means **authorized and eligible to start**, not **already
running**. Never bypass a preflight failure.

## Quick start

Read the [product brief](docs/product-brief.md), [architecture](docs/architecture.md), and [factory setup checklist](docs/factory-setup.md). Copy the required templates into the product repo, fill the blanks, run the validator, then start with the [walking skeleton](docs/operations/walking-skeleton.md). Do not create a ticket backlog before its staging URL works.

For the complete transition and ownership rules, see
[`docs/workflows/linear.md`](docs/workflows/linear.md),
[`docs/workflows/ticket-flow.md`](docs/workflows/ticket-flow.md), and
[`docs/runbooks/operator.md`](docs/runbooks/operator.md).
