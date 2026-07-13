# Per-ticket flow

The full lifecycle of one ticket, with the two invariants that never bend: **tests are authored before implementation by a different model family**, and **the operator approves from an evidence bundle before merge**.

## Sequence

1. **Operator** moves a ticket to Ready. That is the whole prioritization interface.
2. **Planner** (family A) posts the spec'd description, acceptance criteria, and frozen contract on the ticket. Creates the ticket branch. Ticket → In progress. If the product docs can't answer a question, ticket → Blocked-Escalated with the question instead.
3. **Spec-linter** (family B) lints the ticket text itself — criteria quality, contract coverage, consistency, edge coverage — and appends findings plus one `SPEC-LINT: PASS`/`FAIL` verdict line. FAIL sends the ticket back to the planner (one replan); a second FAIL escalates to the operator. The gate only exists between planning and test authoring — tickets past step 4 never re-lint.
4. **Test-author** (family B) commits failing tests as the first commits on the ticket branch, asserting the frozen contract. Confirms they fail for the right reason. Ticks "Tests written".
5. **Builder** (family A, fresh git worktree on the same branch) implements until tests, lint, and typecheck pass. Never touches test files — CI enforces this. Opens the PR.
6. **CI** runs: lint, typecheck, tests, build, self-referential snapshots, test-immutability check.
7. **Reviewer** (family B) checks test adequacy and spec conformance. Approve, or request changes (max 2 rounds → Blocked-Escalated with a plain-language note). Ticket → Review.
8. **Narrator** posts the evidence bundle from the PR's preview deploy: plain-language summary, preview link, screenshots, criteria table, risk line, cost, rollback note.
9. **Operator** approves from the bundle (or sends back with what's wrong). Approval = merge + staging deploy. Ticket → Done.

## Failure routes

- Contract wrong mid-build → ticket back to Ready; planner re-plans; contract change is a new version, never a silent edit.
- Reviewer deadlock after 2 rounds → Blocked-Escalated; operator picks an outcome from the Narrator's plain-language options.
- Budget cap hit → the wrapper refuses to start (or the adapter's hard budget stop ends the run); whoever launched the run moves the ticket to Blocked-Escalated with the wrapper's message. At pilot stage that's the operator; a dispatcher automates it later.
- Preview deploy broken → bundle not produced; ticket back to builder.
- Defect found after Done → new bug ticket linked to the original (escaped defect); one reopen allowed, second → Blocked-Escalated.

## Branch mechanics

- Branch per ticket: `ticket/<id>-<slug>`.
- Commit order is load-bearing: test commits (author: test-author) first, implementation commits (author: builder) after. The reviewer verifies authorship; CI verifies the builder's commits touch no test paths.
- One PR per ticket, merged only via the operator's approval on the bundle. Protected branch; no direct pushes to main.
