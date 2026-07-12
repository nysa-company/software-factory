# Software Factory Kit

A product-agnostic kit for running an AI software factory: agents plan, build, review, and document work on a Linear board; a human sets priorities and approves from evidence bundles, never from diffs.

Built July 2026 for the Nysa project, factored out so any product can use it. Design decisions and their history live in the NYSA repo (`deliverables/2026-07-11-autonomous-software-factory-brief.md`).

## What's in the box

| Folder | Contents |
|---|---|
| `FACTORY.md` | How to instantiate the kit for a product, plus the onboarding validator checklist |
| `envelope/` | Budget and escalation template — the factory's hard limits |
| `roles/` | Versioned prompt files for the five roles (planner, test-author, builder, reviewer, narrator) |
| `workflows/` | Linear board setup, the per-ticket flow, the evidence rubric |
| `scripts/` | Run wrapper with cost ledger, CLI adapters, kill switch, spend rollup |
| `ci/` | Test-immutability check, branch protection, walking-skeleton pattern, rollback drill, Railway recipe |
| `runbooks/` | Operator runbook: what to do when things break |
| `metrics/` | Per-ticket metrics schema |
| `conformance/` | The Nysa-shaped conformance product — the kit's permanent test bed |

## Core rules (enforced by the kit, not by prompts)

1. Budgets live in the run wrapper and provider console caps. Agents cannot raise their own limits.
2. The builder cannot edit tests — CI fails the PR if builder commits touch test files.
3. Approval happens before merge, from a Narrator evidence bundle.
4. Test author and reviewer run on a different model family than the builder.
5. Two review rounds, then the ticket escalates to a human with a plain-language note.

## Quick start

Read `FACTORY.md`, copy the templates into your product repo, fill the blanks, run the validator checklist, then start with a walking skeleton (`ci/walking-skeleton.md`). Do not create a ticket backlog before the skeleton's staging URL works.
