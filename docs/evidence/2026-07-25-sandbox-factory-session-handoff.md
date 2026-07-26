# Sandbox Factory session handoff — 2026-07-25

This is the durable handoff for the development-only Software Factory work
performed across the long-running concurrency and Nysa MVP session. It is
detailed so a new session can continue from repository evidence instead of
reconstructing decisions from chat history.

## Executive status

- The rolling MVP roadmap delivered and merged **9 of 10 planned backend
  tickets**: T-063 through T-071.
- T-072 was canceled by the operator. It has no product PR and no accepted
  implementation source. Its final test branch and diagnostics were archived
  before all retained T-072 lanes were cleaned.
- Earlier development-lane pilots also delivered T-039–T-044, T-050,
  T-052–T-062, and the independent T-073 evaluation slice.
- The strongest measured clean lifecycle in the rolling batch was T-068:
  **42 minutes 34 seconds**, with first-round Spec-linter PASS, first-round
  Reviewer APPROVE, and no successful-role replay.
- A validated four-ticket Contract 1.7 batch completed in **4,239 seconds**
  (70 minutes 39 seconds), observed provider overlap of four, and had zero
  successful-role replay. See
  [Contract 1.7 development evidence](contract-1.7-development-concurrency-2026-07-24.md).
- The sandbox Factory branch contains the demonstrated fixes but remains
  **unmerged and unpromoted**. No Factory release was certified, registered,
  activated, or installed in production.

The product now has backend pieces for transcript ingestion, production
extraction, durable processing, retrieval-backed context, meeting intelligence
reads, manual retry and verification, capture routing, approvals, Gmail
draft/send adapters, approved-action execution, knowledge routing, and entity
proposal resolution. Production pipeline composition remains the missing final
integration ticket.

## Repositories and authorities

### Software Factory development worktree

- Worktree:
  `/Users/sofiagonzalez/Projects/nysa-company/.isolated/software-factory-cli4-20260724`
- Branch: `feat/sandbox-cli4-pilot`
- Pre-handoff HEAD: `44d6f6e` (`fix: invalidate drifted resume approvals`)
- Later-work baseline: `94d1108` (`Document development concurrency evidence`)
- This report and the memory update are committed on the same local branch.
- The branch was not pushed, merged, released, certified, registered, or
  activated by this handoff.

### Nysa application and production boundary

- Product repository: `nysa-company/nysa-app`.
- Product publication used protected pull requests and serial merges.
- The ordinary checkout at
  `/Users/sofiagonzalez/Projects/nysa-company/nysa-app` had unrelated user
  changes and was not modified by this handoff.
- Product work used disposable ticket/batch worktrees and was exported only
  after Factory evidence permitted publication.
- The work did not inspect or mutate production Factory lifecycles, active
  releases, registry, runtime, ledgers, locks, services, Hermes production
  profiles, LaunchAgents, gateways, or production credentials.
- Product `main` changed only through explicitly authorized Nysa pull requests.

## Chronological outcome

### 1. Isolated concurrency foundation

The session began with the production observation that several ticket
worktrees could stay open while a product-wide provider lock serialized actual
model calls. Queue delay then consumed dispatch leases and turned ordinary
waiting into maintenance work.

The development lane reused the existing coordinator and provider runtime
rather than adding another orchestrator or queue. It established:

- owner-only disposable lane roots;
- lane-local homes, runtime, locks, ledgers, credentials, worktrees, ports,
  temporary directories, route plans, and approval hashes;
- coordinator-owned capacity admission;
- independent provider process groups and targeted cancellation;
- durable GO and conservative accounting;
- explicit resume at the failed role instead of hidden retries;
- trusted-host state transitions, commits, and pushes;
- rejection of provider-created remote changes;
- production-path sentinels and fail-closed path containment.

The first deterministic four-ticket mock proof observed a 2.02-second
four-call overlap and completed four synthetic six-role lifecycles in
259 seconds. Mock runs were network-denied and below the 15-minute ceiling.

Main implementation and verification entry points:

- `scripts/factory-dev-lane.sh`
- `scripts/provider-coordinator.py`
- `scripts/provider-cli-runtime.py`
- `scripts/provider-activation.py`
- `scripts/provider-isolated-run.py`
- `scripts/run-agent.sh`
- `scripts/adapters/{codex,claude-code,cursor-agent}.sh`
- `ci/factory-dev-lane-test.sh`
- `ci/provider-coordinator-test.py`
- `ci/provider-activation-test.py`
- `ci/attempt-cancel-test.py`
- `docs/factory-development-lane.md`

### 2. First real four-ticket batch: T-039–T-042

| Ticket | Business outcome | PR | Merge commit |
| --- | --- | --- | --- |
| T-039 | Harden capture webhook handling. | [#180](https://github.com/nysa-company/nysa-app/pull/180) | `3593eda2e9ead90ed5c5460b5d0d3cd8d2e2a052` |
| T-040 | Transactional transcript ingest and extraction storage. | [#181](https://github.com/nysa-company/nysa-app/pull/181) | `a0e0a07a693667be614afe7560eefa2857d3f3a3` |
| T-041 | Safe embedding-version activation. | [#182](https://github.com/nysa-company/nysa-app/pull/182) | `355d8cb80e0fc9eef070e26fe12986394bb55599` |
| T-042 | Durable connector execution records. | [#183](https://github.com/nysa-company/nysa-app/pull/183) | `c187979521b68802a194f2f637b3c60b060b96b2` |

This batch proved that real Nysa work could traverse the development lane with
subscription CLIs and be exported through protected product PRs. It also
exposed the largest productivity problem: a ticket could replay successful
roles after a later role failed. Other problems were strict Spec-linter loops,
expiring authorization/budget boundaries, and different sequencing
interpretations between development and Hermes.

The resulting direction was:

- resume, never silently retry;
- retain completed roles and rerun only the exact current role;
- require deterministic Reviewer repair ownership;
- let the shared coordinator own admission and waiting;
- use the same reconciliation and role sequence in development and Hermes;
- preserve Contract 1.6 behavior and the serialized fallback.

### 3. Focused recovery pilot: T-043 and T-044

| Ticket | Business outcome | PR | Merge commit |
| --- | --- | --- | --- |
| T-043 | Persist transcript speaker attribution. | [#185](https://github.com/nysa-company/nysa-app/pull/185) | `d3dc8cedaaa6c918b9771ba149d64596cbe76681` |
| T-044 | Add an allowlisted fake email connector with no real sends. | [#184](https://github.com/nysa-company/nysa-app/pull/184) | `cdbf9ed76f1b74f72d66142837d4531aa4b4c468` |

T-043 resumed at its failed Spec-linter stage instead of replaying Planner.
This reinforced ticket-bound one-use authorization for extra semantic rounds
and the distinction between a real contract/security blocker and a warning
that should not return to Planner.

### 4. Four-ticket productivity proof: T-050, T-052, T-053, T-054

Detailed evidence is in
[Contract 1.7 development evidence](contract-1.7-development-concurrency-2026-07-24.md).

| Ticket | PR | Merge commit |
| --- | --- | --- |
| T-052 | [#191](https://github.com/nysa-company/nysa-app/pull/191) | `553a10b3ba27fa515779256474fe05c7e9a3ff02` |
| T-053 | [#190](https://github.com/nysa-company/nysa-app/pull/190) | `d426294adbb64f4ee2dadc654f281163573a6947` |
| T-050 | [#192](https://github.com/nysa-company/nysa-app/pull/192) | `53d3f7333be47e0660b835c881db9f0471785f49` |
| T-054 | [#193](https://github.com/nysa-company/nysa-app/pull/193) | `bbcc2acaebef86c1ba1414be7ae1f8339b3ec294` |

Measured results:

- Four-ticket batch: 4,239 seconds, 26 provider attempts, maximum overlap 4,
  zero successful-role replay.
- Corrected two-ticket batch: first terminal boundary in 3,511 seconds.
- T-054 targeted repair: 1,126 seconds; only Test-author repair and Reviewer
  reran.
- Each ticket completed its active lifecycle within an upper bound of
  77 minutes; average upper bound was 69 minutes.
- Against the observed greater-than-16-hour/four-ticket baseline, this was at
  least 71% faster and 3.4 times faster per ticket.
- The 15-minute recovery target was still missed by 3 minutes 46 seconds.

### 5. Transcript and meeting backend wave: T-055–T-062

| Ticket | Business outcome | PR | Merge commit |
| --- | --- | --- | --- |
| T-055 | Production transcript extractor. | [#195](https://github.com/nysa-company/nysa-app/pull/195) | `2060157a6a60d588eb88e0a31ad8a13fac4034f0` |
| T-056 | Durable meeting-processing worker. | [#194](https://github.com/nysa-company/nysa-app/pull/194) | `cb8e7b84c12ad63369beffd864915668c5291b46` |
| T-057 | Retrieval-backed harness context. | [#197](https://github.com/nysa-company/nysa-app/pull/197) | `925d370924a91bbf0d8a527dd67b225f61306e2a` |
| T-058 | Meeting-intelligence read API. | [#196](https://github.com/nysa-company/nysa-app/pull/196) | `bef151b62d0de76048d26478bc0d4ad87fbedcb9` |
| T-059 | Meeting-processing status. | [#199](https://github.com/nysa-company/nysa-app/pull/199) | `1ddda8a04763238729bc2d4843f469f21a1ba105` |
| T-060 | Bounded meeting-processing pool. | [#198](https://github.com/nysa-company/nysa-app/pull/198) | `14b8d250166b98ebc52db2a3e6a72643b0f4691b` |
| T-061 | Safe meeting-processing retry API. | [#201](https://github.com/nysa-company/nysa-app/pull/201) | `22176786a3192e8f937da446ead77ab2b00a799f` |
| T-062 | Verify meeting knowledge candidates. | [#200](https://github.com/nysa-company/nysa-app/pull/200) | `468e6e876ee2206fa32414e40964898b7d7ea76f` |

T-057 and T-058 needed narrow product rulings before Planner could proceed.
The safe rulings reused existing retrieval/embedding seams and limited the read
API to already-ingested meetings instead of inventing a subsystem.

T-057 crossed a UTC accounting day while drained. Checkpoint export had
incorrectly inherited the ordinary same-day resume gate. The corrected path
allows spend-free export across that boundary while the v5 successor retains
historical charges and consumes a fresh current-day authorization.

### 6. Rolling three-ticket MVP roadmap: T-063–T-072

A ticket owned one of three slots from Planner start through confirmed merge.
A new ready ticket could start as soon as a sibling reached
`AWAIT-OPERATOR`, passed publication gates, and merged. Siblings did not wait
for the full cohort. Shared-file and schema merges remained serialized.

| Ticket | Business outcome | PR | Merge commit |
| --- | --- | --- | --- |
| T-063 | Transcript-upload fallback without a bot integration. | [#204](https://github.com/nysa-company/nysa-app/pull/204) | `ea0c3753a9b474236e1750bff9b7be645f2ccc59` |
| T-064 | Route capture intelligence into actionable records. | [#202](https://github.com/nysa-company/nysa-app/pull/202) | `888a900a2ca1b0b443c6e767347390d38d5803ac` |
| T-065 | Gmail draft connector behind existing seams. | [#203](https://github.com/nysa-company/nysa-app/pull/203) | `174fb2f705d9055f8b7700ec656379016a41fa48` |
| T-066 | Workspace-scoped pending-approvals API. | [#206](https://github.com/nysa-company/nysa-app/pull/206) | `0307a6003cebeaebde11748a078f5f606ef1c8c3` |
| T-067 | Exactly-once approval decision API. | [#210](https://github.com/nysa-company/nysa-app/pull/210) | `42d6f64776e545d95688171ee626ec4bbea63464` |
| T-068 | Idempotent approved-action worker with receipts. | [#211](https://github.com/nysa-company/nysa-app/pull/211) | `3ecb9fa3f76875cf9c0caf39e4a925f768f0583b` |
| T-069 | Allowlisted Gmail send; uncertain sends do not auto-retry. | [#208](https://github.com/nysa-company/nysa-app/pull/208) | `9778ef83059cb624ff867bf606f75c69f725e260` |
| T-070 | Route knowledge candidates into verification. | [#209](https://github.com/nysa-company/nysa-app/pull/209) | `6fbe450c4440e79fe004a0bffe5fd3595d3e05c2` |
| T-071 | Resolve entity proposals without guesses or duplicates. | [#205](https://github.com/nysa-company/nysa-app/pull/205) | `c8d9b9a3a5894c3bf9dcd48c4207ac8db89a6725` |

The initial cohort was T-063, T-064, and T-065. Publication and replacement
rolled forward independently according to dependency readiness and shared-file
ownership. This avoided making finished tickets wait for the slowest sibling.

T-073 was an independent capture-pipeline evaluation slice, not one of the ten
roadmap tickets. It merged through
[#207](https://github.com/nysa-company/nysa-app/pull/207) at
`11f95aa68cdf1d17e83b457e58532449cb5e4f0d`.

The rolling work also exposed that local readiness could run broad tests
without first enforcing tests-first immutability. The focused repair reused
the existing check and merged through
[#212](https://github.com/nysa-company/nysa-app/pull/212) at
`e558d7658d919d26942f5864f3a66648a0bb40f7`.

## T-072 cancellation and restart point

### Intended outcome and final state

T-072 would compose transcript fetch, intelligence routing, knowledge routing,
entity resolution, and approved-action execution under shared cancellation and
graceful drain. It was a composition function, not another scheduler, service,
queue, or persistence layer.

- Operator disposition: canceled for now.
- Final state: `Blocked-Escalated`; resume state `Building`.
- Product PR: none.
- Accepted implementation source: none.
- Final branch head: `e2a9dadd1ec588bf2cd2a01f0f69d782255edc03`.
- Relevant commits:
  - `dd1b09d` — failing acceptance tests;
  - `27703d0` — committed contract blocker;
  - `e2a9dad` — trusted state transition.

The Builder removed its provisional `apps/api/src/production-pipeline.ts`
before committing the blocker, so incomplete source is not presented as valid
product output.

### Demonstrated blocker

The focused workspace was `ws-production-pipeline-t072`. Transcript ingest had
to persist a `decision` knowledge candidate. The database trigger validates
category payloads by `(workspace_id, category)`, but only `ws-default` had the
`decision` row in `knowledge_categories`.

The focused test failed closed with SQLSTATE `23514`:

`knowledge payload violates category schema`

The Builder correctly refused to change migration or persistence behavior
because the frozen ticket owned only composition source and its focused test.
The narrow future ruling is to allow T-072's owned fixture to seed the existing
`decision` category definition for the focused workspace. That should change
no product schema, migration, runtime persistence, or subsystem.

A future session should create a fresh development checkpoint/lane from current
product `main` and explicitly decide which semantic roles are reusable. It
must not silently reinterpret the failed Builder attempt.

### Final attempts and archive

| Role | Run | Adapter | Result | Evidence |
| --- | --- | --- | --- | --- |
| Test-author | `1785026239-58947` | Cursor Anthropic | Succeeded; `dd1b09d` | 7 turns; conservative $10; 1,054-second provider interval |
| Builder | `1785027323-70915` | Codex | Contract-blocked | 1 turn; $6.8223; 654-second provider interval |

The final two-role batch took 1,775 seconds, with no role replay and maximum
overlap one because the stages were sequential.

Owner-only archive:

`/Users/sofiagonzalez/Projects/nysa-company/.isolated/checkpoints/rolling-mvp-t072-cancelled-20260726T0115Z`

It contains the complete `T-072.bundle`, ticket, route plan, timing, two run
metadata files, and the Test-author/Builder controller logs.

| File | SHA-256 |
| --- | --- |
| `T-072.bundle` | `5898ed65d14ddb999b0e10c2af43015a67bde96e858b3bcd38f713260ee8ef36` |
| `ticket.md` | `55f6062d1db109c35c3f813e082fd21e7d76e2db3c1f665f178384ef923c2d86` |
| `route-plan.json` | `b10fe08417656c5f189a23c913841c988ccb2a1cb0929ed191cd99e169bce15e` |
| `product-timing.json` | `867a1af92ec786d6937612007d98e685e06f575251d721792d286c571aa7b31c` |

Earlier restart checkpoints:

- `/Users/sofiagonzalez/Projects/nysa-company/.isolated/checkpoints/rolling-mvp-t072-midnight-20260726T0000Z`
- `/Users/sofiagonzalez/Projects/nysa-company/.isolated/checkpoints/rolling-mvp-t072-cursor-path-20260726T0010Z`

After archive verification, trusted cleanup removed:

- `/private/tmp/nysa-sf-dev.2qBSHI`
- `/private/tmp/nysa-sf-dev.jFm0HO`
- `/private/tmp/nysa-sf-dev.AMNKDc`

No process remained bound to those roots.

## Factory challenges, root causes, and fixes

### Hidden retries and successful-role replay

**Problem:** Development retry behavior could restart successful siblings or
earlier roles after a later role failed.

**Fix:** The scheduler stops only the failed ticket and emits an explicit
resume handoff. `product-resume-plan` binds the current stage, completed roles,
retained root, envelope, and one-use approval. Targeted resume selects only the
intended original tickets.

Primary files: `scripts/factory-dev-lane.sh`, `scripts/ticket-state.sh`,
`ci/factory-dev-lane-test.sh`, and `docs/factory-development-lane.md`.

### Coordinator versus scheduler capacity

**Problem:** The development scheduler independently interpreted account and
family capacity, risking disagreement with the shared coordinator.

**Fix:** Admission, waiting, account/family/global limits, timeout, and
cancellation remain coordinator-owned. The lane supplies policy and a bounded
wait, not a second capacity algorithm. Waiting is pre-GO, zero-charge, and
does not retain the launch lock.

Primary files: `scripts/provider-coordinator.py`,
`scripts/provider-activation.py`, `scripts/factory-dev-lane.sh`,
`ci/provider-coordinator-test.py`, and `ci/provider-activation-test.py`.

### Reviewer ownership and Spec-linter loops

**Problem:** An ambiguous Reviewer `FIX` could be translated to Builder even
when Test-author owned the repair. Spec warnings and wording preferences could
also send work back to Planner without changing a security, tenancy,
external-effect, data-loss, or contract boundary.

**Fix:** Contract 1.7 carries deterministic repair ownership and shared
reconciliation records the next role. Ambiguity stops. Spec warnings are
recorded but do not return to Planner; only a genuine contract defect blocks.
Contract 1.6 compatibility is retained.

Primary files: `scripts/lib/reviewer-verdict.py`,
`scripts/ticket-state.sh`, `roles/spec-linter.md`,
`docs/workflows/ticket-flow.md`, and their focused tests.

### Durable commits from failed provider attempts

**Problem:** Claude can exceed a CLI budget after a mutating role already
committed clean output. The wrapper must charge and reject the result, but
blindly resetting it would destroy useful diagnostics.

Observed T-072 examples:

- a failed Test-author output at commit beginning `35c5c6de` reported
  $10.2822515 against a $10 ceiling;
- a later failed Test-author output at `b740121` plus `934024d` reported
  $15.0668695 against a $15 ceiling.

**Fix:** Explicit resume recognizes only an exact latest `provider_failed`
linear history, archives the failed head under
`refs/factory-dev/discarded/<ticket>/<run>` with an owner-only receipt,
restores the unchanged isolated origin, keeps the charge immutable, and reruns
only the failed role.

Relevant commits: `dbfe932`, `0d35738`, and `b309e93`.

### UTC rollover, accounting lineage, and approval drift

**Problem:** A drained lane crossed UTC midnight. Same-day resume correctly
refused the stale budget day, but a first recovery attempt created successor
lineage in a different directory. The accounting CAS root is intentionally
local to the original artifact directory. Separately, retained resume inputs
could drift after approval and before GO.

**Fix:** Reuse checkpoint/export. Successor `accounting-v5-r2.json` and
`lineage-v5-r2.json` stay beside the original lineage and bind the parent.
Pre-GO construction/CAS failure creates no charge. Resume basis is rechecked
before GO; drift archives and removes the unused approval, restores the
original selection, and stops. A proposed large in-place rollover subsystem
was rejected as unnecessary.

Relevant commits: `0d6d990`, `b9a7233`, and `44d6f6e`.

### Cursor isolation and path length

**Problem:** Concurrent Cursor attempts needed independent home, config, data,
temp, credential, and callback state. A legacy shared bridge could not be
Contract 1.7 authority. Later, the first T-072 Cursor fallback used a data path
78 characters long against a 75-character limit; its `data/projects` path was
87 characters against an 84-character limit.

**Fix:** Contract 1.7 Cursor attempts receive isolated roots, native-only lanes
do not depend on the legacy bridge, and safe legacy fallback remains separate.
Attempt data now uses short lane-local `$lane/c/$attempt` roots. The path
failure was pre-GO, cost zero, and released its reservation.

Relevant commits: `45b15d0`, `7789f04`, `b5a341e`, `0222cc8`, `1641b25`,
and `e6c9e70`.

### Native Claude isolation and fallback

Native Claude needed attempt-local `HOME`, `CLAUDE_CONFIG_DIR`, `TMPDIR`,
credential material, sockets, process groups, and cleanup. macOS socket path
limits and nested sandbox behavior required focused changes, not a new runtime.

Relevant commits include `d3471dc`, `71920d8`, `411ee31`, `c21449c`,
`99ccdbf`, `12da1fb`, `3367efe`, `bb0c94e`, and `83d4e39`.

The fallback rule waits up to five minutes for interactive subscription
authorization, then selects another authenticated eligible route without
blocking siblings. It does not bypass identity, budget, isolation, or
role-family policy.

### Provider-created pushes

**Problem:** A provider role that pushes itself leaves remote state outside
the trusted controller's evidence boundary.

**Fix:** Mutating roles may commit but not push. `scripts/run-agent.sh` rejects
remote mismatch; the trusted host performs the exact non-force push after
validating role output. Product publication is a separate trusted action.

### Bootstrap and pre-provider latency

**Problem:** Node setup, CLI probes, immutable-history checks, and terminal
validation consumed material time before the model started.

**Fix:** Lanes bootstrap Node 22 dependencies once per ticket worktree, reuse
pinned CLI versions where safe, and retain exact executable/hash validation at
the provider boundary. Relevant commits are `82bb829`, `a4276c7`, and
`364c1ae`.

### Rolling publication

**Problem:** Fixed cohorts made completed tickets wait for the slowest sibling,
while later branches accumulated refresh/review work as protected main moved.

**Fix:** Export at `AWAIT-OPERATOR`, publish and merge after checks, then fill
the slot from newest main. Product merges remain serial. Affected siblings
refresh only at role boundaries and preserve successful roles. Relevant
commit: `170b4df`.

## Development branch delta

From `94d1108` through pre-handoff HEAD `44d6f6e`, the branch added 42 focused
commits across 21 files: approximately 2,126 insertions and 220 deletions. The
largest file remains the existing `scripts/factory-dev-lane.sh`; no second
scheduler, queue, metrics database, dashboard service, or orchestrator was
added.

```text
d3471dc isolate concurrent Claude subscription runs
759f800 skip Claude cleanup in Codex canary
71920d8 bind Claude hard-coded temp root per attempt
411ee31 fall back when Claude authorization expires
83fac28 bind Claude readiness fixture to its kit
4b740bc model live Claude readiness contract
018b50e derive sandbox cap from product envelope
8eb7243 document bounded subscription authorization fallback
0d6d990 persist resumable sandbox budget snapshot
47feca7 resume operator-resolved blocked tickets
fc938e9 admit blocked state before resume materialization
2809511 keep resume fixture extraction stable
b7318d2 shorten isolated ticket recovery and export
b9a7233 allow drained checkpoints across budget days
a13b4a3 bind checkpoints to exported branch heads
364c1ae resume with fresh isolated credentials
9d2930b reconcile safe Cursor review callbacks
1d84909 document safe Cursor callback reconciliation
a4276c7 bound development role checks
5f0b2a7 decouple native product lanes from Cursor bridge
d402ee0 bind native-only resume fixtures
349c481 cover native-only scheduler fixtures
c21449c isolate Claude setup tokens per lane
99ccdbf allow Claude attempt-local control sockets
12da1fb keep Claude socket paths below macOS limit
bb0c94e keep Claude role tools under Factory Seatbelt
83d4e39 decode native Claude reviewer verdicts
170b4df export retained siblings after targeted resume
3367efe allow Claude attempt-local Unix sockets
7b69bfb probe Seatbelt Unix sockets with system Ruby
3a52d55 isolate Unix socket probe runtime
37d43dd keep Unix socket probe below path limit
45b15d0 isolate concurrent Cursor attempts
7789f04 contain expected Cursor adapter failure
b5a341e allow native-only development lanes
0222cc8 decouple isolated Cursor from legacy bridge
dbfe932 recover failed durable role output before resume
0d35738 document failed-role resume recovery
b309e93 validate discarded role evidence paths
1641b25 shorten isolated Cursor attempt paths
e6c9e70 test realistic Cursor lane path
44d6f6e invalidate drifted resume approvals
```

## What improved and what did not

Demonstrated:

- real provider overlap of four;
- a clean four-ticket batch below 90 minutes;
- targeted recovery that preserves successful work;
- immediate publication rather than full-cohort waiting;
- shared fail-closed admission, cancellation, and accounting;
- better isolated Cursor and Claude attempts;
- rejected provider pushes and accepted trusted-host publication;
- preserved Contract 1.6 and serialized fallback;
- separate product-publication and Factory-promotion authorities.

Remaining gaps:

- T-072 showed incomplete focused seed data can survive Planner, Spec-linter,
  and Test-author. Contract coverage needs required workspace fixtures.
- T-072's Test-author consumed nearly 18 minutes and produced a large focused
  test. Future roles should prefer the smallest acceptance slice.
- Recovery can exceed 15 minutes when interactive authentication or a new UTC
  accounting day is involved.
- Development Cursor concurrency is proven, but production promotion still
  requires an exact canary/topology decision.
- Native Claude authentication can expire. Fallback must remain an
  authenticated subscription route, not an API-key shortcut.
- Warning-driven Spec-linter rounds should continue to be measured.
- The branch records several pilot iterations. Any production proposal must
  test and review the exact branch SHA.

## Recommended next session

1. Read this report, the
   [Contract 1.7 evidence](contract-1.7-development-concurrency-2026-07-24.md),
   `docs/factory-development-lane.md`, `docs/architecture.md`, and
   `context/memory.md`.
2. Treat T-072 as canceled. Do not reuse its approval or deleted runtime.
3. If composition becomes a priority, create a new contract version or
   replacement ticket with only the narrow fixture ruling above.
4. Run focused Factory gates on this exact committed branch. Do not run local
   full CI or local AI review unless separately requested.
5. Choose separately between another small development pilot and a production
   promotion review. Promotion requires an explicit go/no-go.
6. Any promotion review must require protected-main Factory CI, exact-SHA
   sealing/certification, a disposable subscription canary, drained
   maintenance proof, health checks, and rollback evidence. Preserve Contract
   1.6 serialization as rollback.

## Handoff verification

This handoff changes documentation and durable memory only. It should pass
`git diff --check`, managed changed-or-defer readiness, `scripts/repo-check`,
and `scripts/secret-scan`. No remote publication is part of this handoff.
