# Sandbox Factory rolling-ten recovery handoff — 2026-07-26

This is the canonical handoff for the next session. It supersedes the
execution recommendations in
`2026-07-25-sandbox-factory-session-handoff.md` where they conflict, but it
does not replace that report's historical evidence.

The next session should use this document and repository evidence as its
authority instead of reconstructing decisions from the previous chat.

## Goal

Prove that the sandbox Software Factory can keep a rolling window of up to
four concurrent Cursor-backed product tickets and automatically merge ten
accepted Nysa product tickets, with the final four accepted tickets completing
successfully under one unchanged executable Factory SHA.

The ordinary successful path stops immediately after the tenth accepted merge
is recorded and the final-four gate passes.

This is a sandbox qualification. It does not authorize promotion of the
Factory branch, a Factory release, production Factory activation, production
deployment, or external Gmail/Calendar actions.

## Definition of success

All of the following are required:

1. The qualification evidence records ten accepted product merges. T-074,
   T-078, and T-077 are the first three after the T-077 reconciliation
   described below.
2. Every accepted product PR used protected GitHub checks and automatic squash
   merge. Product merges remain serial even while ticket work is concurrent.
3. The final four accepted tickets ran under the same exact executable Factory
   SHA. No Factory code or runtime configuration changed between the first
   role start in that final group and the fourth confirmed merge.
4. The final four required no operator-authored product code, tests, Factory
   code, runtime scripts, configuration, screenshots, or visual executors.
   Documentation and evidence corrections are allowed.
5. No successful role was replayed. A built-in exact-role transient retry or
   the ordinary Reviewer-owned targeted repair path is allowed; restarting a
   completed lifecycle is not.
6. At least one interval contains four genuinely overlapping Cursor provider
   processes. Four occupied ticket slots without four overlapping Cursor calls
   is useful throughput but does not prove the requested provider concurrency.
7. Each final-four ticket targets sixty minutes from first role submission to
   `AWAIT-OPERATOR`. A ticket exceeding ninety minutes is not a clean
   final-four result.
8. GitHub owns broad verification. No local full Factory CI, full product CI,
   full Hermes suite, or local AI review is run.
9. The exact PR, merge SHA, elapsed time, provider attempts, charged amount,
   maximum provider overlap, recovery actions, and any residual risks are
   recorded before qualification is declared complete.

If a trust-critical Factory defect makes these statements false, do not
manufacture a pass. Stop the affected work safely, preserve evidence, and
apply the candidate-change rule below.

## Agreed constraints and preferences

- Sofia's dedicated Mac is the execution host and is expected to remain awake.
- Run up to four concurrent Cursor sessions. Cursor is the primary provider
  for every role.
- Direct Codex and Claude CLIs are fallbacks after an eligible terminal Cursor
  availability failure. They are not an API-key shortcut.
- Wait up to two minutes for a busy provider boundary before treating it as a
  scheduling problem.
- Automatic exact-role retries are preferred to manual retry instructions.
- The target is one hour per ticket, not three hours.
- All ten qualifying product tickets use automatic merge.
- Use the Git-authored product backlog and ticket state. Linear is optional and
  must not block this proof.
- A Ready ticket with a missing product decision or unexecutable contract may
  return to Backlog for clarification while siblings continue.
- Spending limits are control and visibility boundaries, not reasons to throw
  away completed work. The current product daily envelope is $1,500. Crossing
  a ticket allowance pauses only the next submission and permits explicit
  bounded reauthorization; it does not restart the ticket.
- Operator finish corrections are documentation-only. Product code, tests,
  Factory code, scripts, configuration, screenshots, and executors must remain
  role-owned.
- Pixel-perfect comparison is not enforced during this qualification.
- Prefer backend-heavy tickets and backend acceptance first when choosing
  among equally valuable eligible work.
- Continue product delivery in the sandbox. Do not promote the Factory until
  the ten-merge proof has passed and Sofia separately authorizes promotion.

## Explicit non-goals

Do not:

- build a second scheduler, queue, dashboard service, metrics database, or
  general orchestrator;
- redo T-077 or send it through another Factory lifecycle;
- run Chrome/Pillow/CDP exact-pixel publication gates;
- block a merge because a screenshot differs from the prototype by pixels;
- run `bash ci/test-all.sh`, a full Hermes suite, or a broad local product
  suite;
- rerun a test merely because tests-first commits were reordered without
  changing the tree;
- replay Planner, Spec-linter, Test-author, or Builder after that role has
  succeeded unless an authenticated contract rule specifically requires its
  exact targeted repair;
- let a stalled ticket hold a rolling slot indefinitely;
- loosen tenancy, authentication, CSRF, data-integrity, external-action, or
  secret-handling boundaries for speed;
- push, merge, release, activate, install, or deploy the Software Factory;
- manually deploy the Nysa application as part of this proof.

## Repositories and exact starting state

### Software Factory candidate

- Worktree:
  `/Users/sofiagonzalez/Projects/nysa-company/.isolated/software-factory-cli4-20260724`
- Branch: `feat/sandbox-qualification`
- Executed Generation 9 candidate:
  `a836e37aa1085ea7c71b4170c4e6391bb02de4ed`
- Candidate commit subject: `Reset stale role evidence in fresh product lanes`

This handoff is committed after that executable candidate, so the branch HEAD
will be a documentation-only descendant. Do not silently reinterpret the
handoff commit as the Generation 9 executable SHA. Generation 9 remains bound
to `a836e37aa1085ea7c71b4170c4e6391bb02de4ed` unless a successor candidate is
explicitly declared.

### Nysa qualification source

- Worktree:
  `/Users/sofiagonzalez/Projects/nysa-company/.isolated/nysa-app-sandbox-qualification`
- Branch: `chore/sandbox-qualification`
- Current local HEAD before T-077 reconciliation:
  `d472e054a0a4e5d07ee2189fc0073f9b0ec4e09d`
- Current protected-main ref in that checkout is stale at T-078 and must be
  fetched before reconciliation.
- Qualification manifest:
  `factory/QUALIFICATION.json`
- Generation result:
  `factory/qualification/generation-9.json`

The product already has `MAX_CONCURRENT_TICKETS=4`. The Generation 9 manifest
uses capacity three until three protected Done results and capacity four
afterward. Recording T-077 as the third accepted merge therefore activates
four-slot dispatch without a new scheduler or Factory change.

### Confirmed Generation 9 product merges

| Ticket | PR | Protected merge SHA | Merged at (UTC) | Recorded now |
| --- | ---: | --- | --- | --- |
| T-074 | 216 | `5c11e8b7c1af3e9197fa5be462a67ef0ee311417` | 2026-07-26 20:33:09 | yes |
| T-078 | 217 | `c6717fb8ee6c9e7a9a78d9817bad1b03c5efcf10` | 2026-07-26 23:34:54 | yes |
| T-077 | 218 | `198cfc4ddf8628a7194e9fed54cec9f99564e915` | 2026-07-27 00:12:22 | no |

PR 218 had successful required GitHub checks and automatic squash merge. Its
absence from the local Generation 9 result is bookkeeping lag, not permission
to rerun T-077.

T-075 and T-076 are valuable merged product work from invalidated Generation
6, but they do not count toward the present ten-ticket proof.

Before launching another role, the next session must:

1. fetch protected product `main`;
2. merge or otherwise reconcile that exact protected head into
   `chore/sandbox-qualification` without rewriting prior qualification
   history;
3. mark T-077 Done through the same trusted product-state/evidence pattern
   used for T-074 and T-078;
4. append PR 218 and its exact merge SHA to Generation 9;
5. derive Generation 9 charge totals from terminal manifests rather than
   guessing;
6. verify dispatch reports three Done and capacity four;
7. never execute a T-077 product role again.

## What consumed the previous 24 hours

The original rolling design was not the main failure. The prior sandbox work
had already demonstrated nine rolling backend merges, real four-provider
overlap, and a clean 42-minute lifecycle. The qualification then combined
Factory development, product delivery, and immutable certification. Every
new harness defect changed the candidate and reset useful evidence.

Generations 1 through 8 were invalidated by:

1. the lane overriding Cursor-first routing;
2. macOS temporary paths exceeding Cursor limits;
3. conservative cost accounting rejecting contract blockers;
4. missing-contract and Spec-lint failures using the wrong terminal state;
5. an indented Spec-lint failure being parsed as resumable;
6. PR-less Narrator handling of visual evidence;
7. Reviewer finding detail not surviving targeted repair;
8. fresh lanes copying stale role-control evidence.

These were mostly Factory qualification-control defects, not eight failures to
implement the same product features.

### T-077

T-077 consumed approximately 3 hours 42 minutes across five retained lanes,
18 role attempts, and about $180 of conservative charges. Its successive
blockers were:

1. an undefined visual executor;
2. the wrong pinned Pillow interpreter;
3. Chrome changing its CDP page target during navigation;
4. a 45-minute Chrome timeout inside the provider sandbox even though trusted
   host capture worked;
5. an exact Narrator deferral phrase being reformatted;
6. publication tests freezing whole shared files and becoming stale after a
   sibling merge;
7. missing prototype references in the publication branch;
8. a viewport configuration defect;
9. an exact-pixel difference after functional implementation was already
   reviewed.

The correct lesson is not to build a more elaborate visual executor during
this proof. Pixel comparison is now advisory, and T-077 is merged.

### CI

Healthy protected PRs generally spent about 2–3 minutes in GitHub checks.
Two PRs took about ten minutes because serialization or stale cross-ticket
tests caused another protected run. Removing local broad CI is correct, but
CI was not the dominant delay. Repeated role lifecycles and over-frozen
contracts were.

## Qualification operating model

### One rolling controller

Reuse the existing development coordinator and product lane. The operator
loop has only five responsibilities:

1. select eligible Ready tickets;
2. keep up to four ticket/provider slots occupied;
3. classify and recover exact failures;
4. publish accepted branches serially through GitHub auto-merge;
5. record evidence and refill each confirmed-merge slot.

No new service is required for this proof.

### Concurrency

- The capacity ceiling is four tickets and four simultaneous Cursor provider
  processes.
- Start four eligible tickets together when four are available.
- A ticket owns its rolling slot from first role submission through confirmed
  protected merge or explicit return to Backlog.
- As soon as a product merge is confirmed, refresh eligibility from newest
  protected `main` and start the next approved ticket.
- Do not wait for a fixed cohort to finish.
- Product publication remains serial to avoid competing merges into `main`.
- Refresh affected siblings only at a role or publication boundary. Preserve
  their successful role evidence.

Measure concurrency from provider GO/terminal intervals in the authoritative
manifests. Do not infer four-way provider overlap from four worktrees or four
shell processes.

### Ticket selection

After T-077 reconciliation, the remaining Generation 9 tickets are T-079
through T-085.

Start with these four:

1. T-080 — Meetings index and tenant-scoped read API.
2. T-081 — Meeting detail and tenant-scoped aggregate read API.
3. T-082 — My Board and existing approvals read surface.
4. T-084 — Knowledge index and tenant-scoped read API.

This first fill favors backend-bearing work and leaves the UI-only import flow
out of the initial four.

Use this replacement pool as dependencies become satisfied:

- T-079 — Fathom import screen;
- T-083 — approval detail and sandbox receipt, after T-082;
- T-085 — Knowledge detail/review, after T-084.

The prototype remains a product reference. For these tickets, acceptance is
based on correct tenant-scoped behavior, accessible structure, responsive
layout, explicit loading/empty/error states, focused tests, and a production
build where applicable. Exact screenshot hashes, zero-pixel differences, and
CDP/Pillow comparison are not acceptance gates.

Do not silently reduce a ticket to backend-only if that changes its approved
outcome. Prefer and implement the backend boundary first, then the smallest
functional UI required by the ticket. A replacement backend ticket requires a
written outcome and Sofia's approval before it becomes Ready; the general
preference for backend work is not blanket approval for an unknown feature.

### Role flow

Keep the existing role separation:

`Planner → Spec-linter → Test-author → Builder → Reviewer → Narrator`

- Cursor is first for each role.
- Cross-model checking/review stays enabled.
- A successful role is immutable evidence and is not replayed.
- Reviewer repair goes only to the named Test-author or Builder owner and then
  returns to Reviewer.
- Narrator/documentation defects do not invalidate reviewed implementation.
- Automatic retry is limited to the exact failed role and the authorized
  provider route. Never disguise a new full lifecycle as a retry.

### Failure classification and authority

Classify before acting:

| Class | Examples | Action |
| --- | --- | --- |
| Transient provider/CLI | terminal availability, short authentication or model-list miss | automatically retry the exact role once or use the authorized direct-CLI fallback; siblings continue |
| Contract ambiguity | missing decision, unexecutable requirement, Spec-lint semantic failure | allow one focused clarification/replan; then return only that ticket to Backlog for an operator ruling |
| Product correctness | failing focused behavior, Reviewer finding | run only the named Test-author/Builder repair and Reviewer verification |
| Documentation/evidence | ticket prose, evidence headings, PR explanation, ledger narrative | operator may correct documentation directly without replaying product roles |
| Publication compatibility | stale branch, sibling-owned behavior test, protected CI failure | refresh from latest main and assign the smallest source/test repair to its owning role; GitHub reruns broad checks |
| Pixel fidelity | screenshot delta, font/rendering variance | record as advisory; do not block or invoke an exact-pixel executor |
| Trust/safety | tenancy leak, auth/CSRF bypass, data loss, external send, secret exposure, accounting corruption | fail closed, drain safely, and repair the root cause before publication |
| Factory core | scheduler, admission, isolation, accounting, role-order, retry/evidence integrity defect | drain affected work, patch the smallest shared root cause, declare a successor candidate, and reset the final-four streak |

Operator documentation authority includes ticket/contract prose,
qualification evidence, PR descriptions, handoff notes, and residual-risk
records. It does not include editing product source, tests, Factory code,
scripts, configuration, screenshots, or visual tooling.

### Time control

- Poll active work at approximately two-minute boundaries; do not block the
  supervising session for long unobserved waits.
- If a provider role produces no credible progress or durable artifact for
  twelve minutes, inspect it once and either cancel/retry that exact role or
  classify the blocker.
- Treat sixty minutes as the ticket recovery boundary. At that point choose
  targeted recovery, a documented operator ruling, or return to Backlog.
- Ninety minutes is the qualification ceiling for a clean ticket.
- A stalled ticket releases its rolling slot after safe drain and Backlog
  return. Siblings continue.
- Do not allow another 45-minute browser or visual-tool experiment. Pixel
  tooling is outside this proof.

### Testing and CI

Inside a product role, run only the smallest focused check needed to prove the
owned behavior. A web ticket may run its focused web test and production
build; an API ticket may run its focused API test. Reuse an exact successful
result when the tree is unchanged.

The following are prohibited locally in this proof:

- full Software Factory CI;
- full Nysa application CI;
- full Hermes verification;
- local AI review;
- broad publication reruns already covered by an unchanged reviewed head.

GitHub runs the complete required protected checks. When GitHub reports a
failure:

1. inspect the exact failing job;
2. when policy and test-immutability are green and only a test job failed,
   rerun failed GitHub Actions jobs once on the unchanged PR head before
   reopening any Factory role;
3. if that exact-head rerun passes, continue publication without a repair;
4. if it fails, identify the owning ticket or stale cross-ticket assertion;
5. apply the smallest role-owned repair;
6. run only its focused local check;
7. push the exact reviewed head and let GitHub rerun the broad suite.

The one-rerun allowance is per PR head. Policy, test-immutability, security,
or other control-plane failures never use it, and a second test failure on the
same head must route to its exact owning role.

Acceptance tests must assert behavior and owned seams. They must not freeze
the complete bytes or hash of unrelated shared files, ban all references to a
concept outside the owned module, or make a sibling merge invalidate correct
behavior.

When protected CI fails after a portable ticket reached operator-await, record
the exact Actions job as `PUBLICATION FAILURE: <job-url>` and name only its
owning `OPERATOR PUBLICATION REPAIR: test-author|builder`. Resume that ticket
from its authenticated checkpoint. Preserve imported roles; require a fresh
Reviewer and Narrator before exporting the replacement publication strata.

### Budget handling

- Keep the product's $1,500 daily envelope visible.
- A ticket reaching its default allowance pauses before another provider GO;
  it does not lose its successful roles or return automatically to the start.
- The operator may issue a bounded, recorded increase and resume only the
  current role.
- Exceeding the $1,500 day threshold requires Sofia's explicit approval. It is
  an authorization boundary, not an instruction to discard work.
- Unknown post-GO cost remains conservatively charged.
- Record all attempts, including failed and fallback attempts, in final
  evidence.

## Candidate and proof-count rule

Generation 9 currently uses executable Factory SHA
`a836e37aa1085ea7c71b4170c4e6391bb02de4ed`. Use it unchanged for the
remaining seven tickets unless a trust-critical Factory defect requires a
root-cause repair.

Documentation-only commits in the Factory repository do not change the
executed candidate. Product code and ticket-contract corrections do not
change the Factory candidate either.

If Factory code or runtime configuration changes:

1. drain and preserve affected lane evidence;
2. commit the smallest root-cause fix with one focused check;
3. declare and pin a successor Factory candidate explicitly;
4. preserve prior accepted product merges in the candidate-lineage ledger;
5. reset the consecutive clean-final-ticket counter to zero;
6. run at least four subsequent accepted tickets under that exact successor
   SHA before claiming stability.

The ordinary plan has seven tickets remaining, so there is room for the final
four without adding work. If a Factory change occurs so late that fewer than
four approved tickets remain before the tenth accepted merge, do not silently
merge extra unapproved work and do not claim success. Pause and ask Sofia
whether to extend the proof or approve replacement backend tickets.

Non-safety parser wording, evidence formatting, documentation, PR narrative,
or pixel-policy corrections do not justify a new Factory candidate and do not
reset the final-four counter.

## Exact next-session sequence

1. Read this file, the affected repositories' `AGENTS.md`, and the current
   `factory/QUALIFICATION.json` plus Generation 9 result.
2. Confirm both worktrees are clean except for intentional handoff/product
   reconciliation commits.
3. Verify PR 218 is merged at
   `198cfc4ddf8628a7194e9fed54cec9f99564e915` with required checks green.
4. Reconcile T-077 into the qualification branch and Generation 9 evidence.
   Do not run a T-077 role or visual comparison.
5. Verify the dispatcher reports three terminal qualification tickets and
   capacity four.
6. Confirm the executable kit pin is
   `a836e37aa1085ea7c71b4170c4e6391bb02de4ed`, Cursor-first routing is ready,
   the daily envelope is $1,500, no production Factory process is targeted,
   and no stale leases/providers remain.
7. Launch T-080, T-081, T-082, and T-084 through the existing rolling
   coordinator.
8. Observe role progress at bounded intervals. Apply the failure table above;
   do not improvise full-lifecycle restarts.
9. At each `AWAIT-OPERATOR`, validate exact reviewed head, focused evidence,
   tests-first history, no forbidden external behavior, and documentation.
10. Open the product PR, enable auto-merge immediately, and let GitHub own
    broad CI.
11. After each confirmed merge, update the qualification branch from protected
    main, record exact evidence, and fill the free slot with T-079, T-083, or
    T-085 according to eligibility.
12. Keep product merges serial while provider work remains concurrent.
13. Before accepting merge seven of ten, identify the four tickets that will
    constitute the final-four evidence and verify their complete role runs use
    the same exact Factory SHA.
14. After merge ten, audit the definition of success. If every condition
    passes, mark the qualification complete and stop. Do not start another
    ticket, promote the Factory, or deploy anything.

## Stop and escalation rules

Stop the affected ticket, not the entire rolling window, for ordinary product
or contract failures.

Stop and drain the whole qualification only for:

- Factory isolation or accounting integrity loss;
- inability to identify or terminate the correct provider process;
- production-path contact;
- credential/secret exposure;
- evidence that accepted merge attribution is false;
- a required Factory code change during the final-four proof.

If the tenth merge arrives but the final-four gate fails, do not label the
Factory stable. Report the exact failed condition and wait for Sofia's decision
on extending or resetting the proof.

## Final report format

The completion handoff should contain:

- final executable Factory SHA;
- all ten accepted ticket IDs, PRs, and merge SHAs;
- start, `AWAIT-OPERATOR`, PR, and merge timestamps;
- per-ticket elapsed seconds and provider attempts;
- maximum observed Cursor overlap with the four overlapping attempt IDs;
- automatic retries, direct-CLI fallbacks, Reviewer repairs, Backlog returns,
  and documentation-only operator corrections;
- local focused checks and protected GitHub check URLs/results;
- charged amount per ticket and total;
- confirmation that no local broad CI/Hermes suite ran;
- confirmation that pixel-perfect comparison was not enforced;
- confirmation that the final four used one unchanged executable Factory SHA,
  had no successful-role replay, and had no operator non-documentation edits;
- confirmation that no Factory promotion or manual deployment occurred;
- remaining risks and the separate promotion decision required from Sofia.

## Evidence references

- `docs/evidence/2026-07-25-sandbox-factory-session-handoff.md`
- `docs/evidence/contract-1.7-development-concurrency-2026-07-24.md`
- `docs/factory-development-lane.md`
- `docs/architecture.md`
- `context/memory.md`
- Nysa `factory/QUALIFICATION.json`
- Nysa `factory/qualification/generation-1.json` through
  `generation-9.json`
