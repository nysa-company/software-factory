# Ticket workflow

The full lifecycle of one ticket, with the two invariants that never bend: **tests are authored before implementation by a different model family**, and **the operator approves from an evidence bundle before merge**.

## Sequence

1. **Operator** moves a ticket from Backlog to Ready in Linear. The reconciler records that transition in the ignored operator overlay; the dispatcher materializes it on the ticket branch and no live API call is made by preflight.
2. **Dispatcher** creates the exact clean ticket branch/worktree from current protected `origin/main`, then runs trusted operator-field materialization to create and verify its remote ref. It invokes sealed `models pin`, which resolves the active profile (or default `balanced-v2`) only if one ordered portfolio has ready candidates for all six roles with distinct production/checking families. The one pushed commit contains both `Kit-SHA` and `factory/route-plans/<ticket>.json`; preflight follows.
3. **Planner** uses the exact production-lane route pinned for Planner, starts Planning, and posts the spec'd description, acceptance criteria, and frozen contract. If the product docs cannot answer a question, ticket → Blocked-Escalated with the question instead.
4. **Spec-linter** uses its pinned checking-lane route, remains in Planning, checks criteria quality, contract coverage, consistency, and edge coverage, and appends findings plus one `SPEC-LINT: PASS`/`FAIL` verdict line. FAIL sends the ticket back to the planner (one replan); a second FAIL escalates to the operator. The operator may authorize only the next semantic lint round with the exact ticket line `OPERATOR AUTHORIZATION: spec-linter round <N>`; the dispatcher never writes it on its own.
5. **Test-author** uses its pinned checking-lane route, starts Building, and commits failing tests as the first commits on the ticket branch, asserting the frozen contract. Confirms they fail for the right reason.
6. **Builder** uses its pinned production-lane route in a fresh git worktree on the same branch and implements until tests, lint, and typecheck pass. Never touches test files — CI enforces this. Opens the PR.
7. **CI** runs: lint, typecheck, tests, build, self-referential snapshots, test-immutability check. The trusted ticket-PR helper waits without launching a role while required checks are pending. Completed failures are mandatory Reviewer evidence for a repair verdict.
8. **Reviewer** uses its pinned checking-lane route in Review and checks test adequacy, CI, and spec conformance. Approve, or request changes back to Building (max 2 rounds → Blocked-Escalated with a plain-language note). Any later Builder or Test-author run invalidates the approval and requires a fresh review.
9. **Narrator** starts only after the ticket-PR helper revalidates successful required checks and the exact current head against the latest successful Reviewer evidence. It uses its pinned production-lane route, remains in Review, and commits the bundle from the PR's preview deploy: plain-language summary, preview link, screenshots, criteria table, risk line, cost, rollback note. Under contract 1.3, the dispatcher then invokes trusted `ticket-attest --action bundle`; it verifies the exact reviewed SHA, every successful run's tuple against the committed six-role route plan, the route-plan blob/digest and policy hash, allowed post-review paths, bundle blob, and unique exact PR before committing Awaiting Approval. Contract 1.2 still stops in Review.
10. **Operator** moves Awaiting Approval → Approved in Linear. The reconciler records the exact observation and Linear update times. Trusted `ticket-attest --action approval` accepts only an approval newer than the unchanged bundle attestation, commits Approved and its binding attestation, and requests ordinary protected GitHub auto-merge with the repository's exact configured method for the exact current PR head. A failed or unconfirmed request retains the overlay for a receipt-valid retry and never bypasses protection.
11. After GitHub merges, the dispatcher prepares exact clean `chore/tNNN-closeout` with `HEAD == origin/main`. Trusted `ticket-attest --action done` requires the merged PR head to be the approval receipt commit, validates protected bundle/approval blobs, and requires every configured exact post-merge context successful and unambiguous on the merge commit. It projects the ledger once, writes and pushes one Done closeout commit, creates or reuses the exact factory-owned metadata/accounting PR, and requests protected auto-merge with the configured method. Retries revalidate that exact commit and converge PR creation/auto-merge without another projection or commit. No second business approval is required.
12. Only after the closeout PR merges and the strengthened protected-main reader validates Done does `next-stage` return `COMPLETE`. The dispatcher then invokes trusted `release` with the matching opaque lease. Linear sync projects Done from protected main. At concurrency two, every attestation action receives the lease in memory; it is never released merely because a closeout PR exists.

Roles never re-resolve the portfolio. Before submission, the wrapper re-probes
only the role's exact pinned route. Once any task-bearing CLI starts, every
failure is terminal for that run; the dispatcher escalates instead of launching
another route.

Every mutating role must finish with a new commit and a clean ticket worktree;
the trusted wrapper non-force pushes that commit and verifies the remote tip.
Reviewer is read-only and must leave the branch, HEAD, and worktree unchanged.

## Failure routes

- Contract wrong mid-build → ticket back to Planning; planner re-plans; contract change is a new version, never a silent edit.
- Spec-lint or Reviewer deadlock after 2 rounds → Blocked-Escalated; the operator may authorize only the next semantic round with the exact role-specific ticket line, or pick another outcome.
- Budget cap hit → the wrapper refuses to start (or the adapter's hard budget stop ends the run); whoever launched the run moves the ticket to Blocked-Escalated with the wrapper's message. At pilot stage that's the operator; a dispatcher automates it later.
- Confirmed temporary credit exhaustion → operator may add a TTL-bound `credits_exhausted` override for the narrow account-route, family, model, or route scope, then pin a new ticket. Existing ticket plans do not change. Subscription quota telemetry is incomplete and cannot safely automate this judgment.
- Preview deploy broken → bundle not produced; ticket back to builder.
- Defect found after Done → new bug ticket linked to the original (escaped defect); one reopen allowed, second → Blocked-Escalated.
- Linear unavailable → in-flight execution continues from Markdown; new priority, Ready, approval, or unblock actions wait for a successful reconciliation cycle.

## Branch mechanics

- Branch per ticket: exactly `<TICKET_BRANCH_PREFIX><T-NNN>` (default `ticket/T-NNN`), with no slug or suffix.
- Commit order is load-bearing: test commits (author: test-author) first, implementation commits (author: builder) after. The reviewer verifies authorship; CI verifies the builder's commits touch no test paths.
- One PR per ticket. The single business approval is the Linear bundle approval; it enables protected auto-merge without bypassing required checks or conflicts. No direct pushes to main.
