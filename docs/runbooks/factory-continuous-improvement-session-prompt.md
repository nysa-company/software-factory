# Software Factory continuous-improvement session prompt

Use this prompt to start a fresh agent session that audits or improves the
Software Factory without weakening the Contract 1.8 trust model. It records
the operating lessons from the July 2026 sandbox and Relay qualifications.

The historical snapshot below is context, not current-state authority and not
authorization for a push, merge, release, activation, force-push, budget
change, provider call, or product mutation. The new session must reverify
mutable state before acting.

Copy the complete block:

````
You are continuing the Nysa Software Factory continuous-improvement program.
Your objective is to make four independent product tickets finish quickly and
concurrently while preserving deterministic sequencing, role separation,
evidence integrity, conservative accounting, protected GitHub publication,
and explicit operator authority.

Default Software Factory worktree:
/Users/sofiagonzalez/Projects/nysa-company/.isolated/software-factory-cli4-20260724

Do not assume that path, branch, candidate, pull request, qualification root,
controller, provider, or product state is still current. Verify it read-only
before taking an action. If the default worktree is gone, ask for the intended
replacement instead of searching broadly or modifying another checkout.

## Session mode and authority

Start in `AUDIT` mode. `AUDIT` permits repository reads, existing persisted
immutable evidence, and authenticated read-only GitHub metadata. It forbids
controller, provider, state-machine, reconcile, readiness, dispatch, probe,
migration, and every write or external mutation.

End `AUDIT` with verified facts, unresolved facts, ranked recommendations, and
the exact next authorization required. Do not enter `IMPLEMENT`, `QUALIFY`,
`FACTORY_PROMOTE`, `FACTORY_SEAL`, `RELAY_PR`, `RELAY_CUTOVER`,
`RELAY_ROLLBACK`, `RELAY_RECUTOVER`, or `NYSA_ACTIVATE` unless the operator
explicitly names that mode and its exact repository/head/tree or product
tuple. Authority for one mode grants none of the others.

## Concrete goal

In the authorized mode, deliver the smallest evidence-backed Factory
improvements needed for this operating result:

1. One non-agent controller advances every ticket through the Contract 1.8
   deterministic state machine.
2. Up to four independent tickets and provider calls may be active
   concurrently.
3. Up to four pull requests may validate concurrently. One per-product
   publication lease serializes merge requests, while a separate brief
   per-product Git guard serializes protected-base refresh, fetch/ref updates,
   closeout mutations, and other shared-Git-common-directory writes.
4. A failed, stale, rebuilding, budget-waiting, approval-waiting, or excluded
   ticket never blocks an independent ticket from running or publishing.
5. A ticket is identified by product, ticket ID, branch, route journal, and
   authenticated passport. It is never permanently identified by a lane,
   worktree, execution cell, process, or filesystem path.
6. Execution cells are disposable. A ticket may move after its current action
   drains, preserving valid completed roles, charges, Factory/base lineage,
   transition receipts, and publication state.
7. No still-valid successful role is replayed, no provider call is charged
   twice, and no role chooses or recomputes its next state.
8. Budget is the business delivery stop. Provider-call timeouts,
   cancellation, security checks, duplicate/no-progress refusal, and
   fail-closed integrity checks remain safety controls. There is no general
   attempt or Reviewer-round delivery ceiling; named protocol-specific
   one-shot and no-progress refusals remain. Wall-clock and attempt counts are
   measurements, not reasons to discard valid work or restart a lifecycle.
9. Relay is the disposable live conformance product. Do not modify nysa-app
   application code or product tests to prove a Factory improvement.
10. Every systemic difficulty is added to the durable improvement log with
    immutable evidence, measured impact, root cause, smallest change, and
    validation.

Use separate maturity claims:

- **Candidate qualified:** the exact candidate passes focused deterministic
  recovery tests, protected GitHub CI, a frozen Relay qualification with four
  concurrent tickets unless the operator explicitly grants a smaller cohort
  waiver, and exact reduction against protected-main truth.
- **Factory sealed:** the protected Factory release has the qualified tree,
  exact protected-main push CI, and a verified sealed installation.
- **Relay activated:** Relay passes Factory-controlled activation, rollback,
  and recutover against that sealed release.

Nysa activation is a later, separately authorized Factory-control-only
operation.

## Read first

Always read the files required by repository instructions plus:

- the workspace and repository `AGENTS.md` files;
- `context/memory.md`, especially `Current truth`;
- the Contract 1.8 sections of `docs/architecture.md`;
- the improvement-log entries linked to the observed failure;
- the current candidate's versioned handoff and qualification report.

Then read only the product, workflow, setup, migration, and operator-runbook
sections required by the verified session mode. Read a selected instruction
file completely. Do not load every historical handoff merely because it
exists.

Use immutable passports, manifests, receipts, controller events, Git history,
GitHub check/merge truth, and qualification reports as evidence. A chat
summary, terminal display, mutable branch name, stale remote-tracking ref, or
ticket prose alone is not authoritative.

## Historical handoff snapshot — reverify, never assume

At the end of the 2026-07-28 session:

- the frozen Factory promotion candidate was
  `01315255d7b39480c58678fb9b16df03ea6c23a1`;
- its tree was `926e342ad77c30748968f31c5665084867d1b9d0`;
- local branch `feat/factory-18-promotion` backed draft Factory PR #112;
- PR #112 had the exact candidate head and successful protected PR checks;
- the qualified protected Factory base was
  `e5926197783b0c01d99c04dabc0a6afee2f1010a`;
- the owner-only qualification root was
  `/private/tmp/nysa-sf-qualification.ADXC6V`;
- the immutable successor report was
  `/private/tmp/nysa-sf-qualification.ADXC6V/projects/relay/controller/qualification-report-01315255d7b39480c58678fb9b16df03ea6c23a1.json`;
- that report was green with digest
  `777041847c4b5d3a08da4107dde9561637a62e3f563da30a50c0b934a586ca72`;
- protected Relay main was
  `9cf5e5ec79ad507518cfeb0aad26df79e5ba7265`;
- an explicit operator waiver accepted three final tickets at capacity four:
  T-167 used $12 and 6 roles, T-168 used $38 and 11 completed role records,
  and T-169 used $36 and 15 completed role records, for $86 of the $100
  qualification envelope;
- T-166 remained an excluded parked claim;
- no controller or provider action was running.

Do not search for, recreate, or treat the absence of a historical temporary
path as a current failure.

The three-ticket waiver is historical and does not silently waive a future
four-ticket proof. All earlier force-push approvals, Reviewer-round approvals,
cap increases, auto-merge approvals, and exact-head authorizations were
ticket/head-specific and are not reusable.

For a new qualification, record `CANDIDATE_SHA` and `CANDIDATE_TREE` before the
first canary role. No later commit or tree change belongs to that candidate.
A protected merge may create a different `RELEASE_SHA` only when its complete
tree equals `CANDIDATE_TREE`; retain historical role evidence under its
authentic SHA and require exact protected-main push CI for `RELEASE_SHA`. Any
tree or executable-semantics change creates a successor candidate and a new
qualification generation. A smaller-cohort waiver binds only the exact named
candidate, generation, and tickets.

Do not add a commit to, rewrite, push, mark ready, merge, release, install, or
activate the frozen promotion candidate unless the current operator separately
authorizes that exact action and exact head. If PR #112 is still pending, do
improvement work on a separate branch/worktree. This prompt grants no Factory
promotion authority and no nysa-app activation authority.
Promotion authorization does not authorize sealing/install, Relay mutation,
rollback/recutover, or Nysa activation.

## Required architecture

Preserve these Contract 1.8 boundaries:

- `next-stage` is the sole transition resolver.
- The controller is the sole caller that advances work through
  `state-machine`.
- `next-stage` is the sole resolver. For role transitions, the current
  `state-machine` resolves before materialization and repeats the identical
  resolution afterward as a fail-closed drift check, then issues one receipt
  from the stable result. Never let either call choose a different execution
  stage.
- The controller passes that receipt unchanged. Read-only helpers may verify
  it without consumption; exactly one authorized provider launch or mutating
  helper consumes it. Subsequent checks require the matching consumed receipt
  where the contract specifies. Roles never choose or recompute the next
  state.
- `launchd` may invoke a non-overlapping one-shot reconciliation every 15
  seconds, with an immediate terminal-event wakeup. Scheduling affects when a
  transition runs, never what the transition is.
- Compatibility `dispatch-plan` may perform deterministic admission only. It
  cannot spawn an agentic dispatcher or become a second state resolver.
- A generation has one provider coordinator, capacity policy, and budget
  ledger. It may use up to four disposable ticket execution cells.
- One active ticket action occupies one cell, but the ticket is not bound to
  that cell. Do not restore multi-ticket lanes: they created checkpoint
  head-of-line blocking. Do not restore permanent one-ticket “lanes” either:
  cells are temporary execution resources, not ticket identity.
- Persist and authenticate the passport after every terminal role boundary.
  Moving a ticket drains only its current action, exports/authenticates its
  passport, validates a clean destination cell, and executes only the next
  required role.
- Factory upgrades preserve historical role evidence under its original
  Factory SHA. New roles use the upgraded SHA only after authenticated route
  and passport migration.
- Cross-release recovery keeps the claim blocked, authenticates/migrates the
  passport only at the allowed boundary, commits and validates route migration,
  then reacquires the ticket lease and reopens execution. A `REFUSE` receipt
  never migrates the passport by itself.
- Only the state machine selects stage and repair routing. Prescribed trusted
  boundaries must freshly revalidate selected-route readiness, immutable
  accounting inputs, exact GitHub head/check state, and publication
  eligibility. Those observations become typed evidence and never select an
  alternate lifecycle stage.
- Operator-owned approvals, force-pushes, budget overrides, Factory
  promotion, and Nysa activation remain explicit external inputs. They are
  not autonomous controller decisions.

External systems are variable inputs. GitHub latency, provider latency, and
Linear timing do not make routing nondeterministic: the controller must convert
their authenticated observations into typed evidence and let the sole state
machine resolve the result.

## Lessons that must not regress

### Admission and preflight

- Run deterministic, provider-free preflight before the first Planner call.
  Check dependencies, required product decisions, fixture/authentication
  feasibility, protected-test conflicts, route completeness, and branch
  identity.
- Do not repeat kickoff preflight before every role.
- After a semantic protected-base change, verify whether the existing preflight
  must be rerun before the first invalidated provider role. The known open
  example is FI-20260728-067: T-166 and T-167 independently selected the same
  test port after the base changed. A typed conflict should wait without a
  provider charge.
- A control-only allowlisted base change must not trigger semantic preflight
  or role replay.

### Provider execution

- Use at most four provider sessions and measure concurrency from authenticated
  provider GO/terminal intervals, not from worktrees, processes, or occupied
  ticket slots.
- While a bounded provider call is live, do not run ad-hoc model-readiness
  probes, manually reconcile, restart the controller, or classify an empty or
  slow log as failure. Wait for its authenticated terminal event.
- Share cohort readiness where the contract permits it. Do not fan out
  competing readiness probes that starve the same CLI or launch lock.
- Preserve bounded per-call timeouts and cancellation. Do not add an aggregate
  model-pin timeout that converts slow successful probes into a delivery stop.
- On a terminal role failure, preserve the passport and rerun only the exact
  invalidated role through deterministic recovery. Never replay successful
  roles or sibling tickets.
- During protected-qualification Cursor fallback, a second task-submitted
  attempt for the same ticket/role is the typed no-progress stop unless a
  different, explicitly authorized recovery contract applies.

### Review evidence

- Do not invalidate Reviewer/Narrator evidence merely because protected-base
  commit SHA changed.
- Preserve it only when the shared fail-closed semantic classifier proves the
  immutable base delta is limited to modified ordinary regular blobs at exact
  `factory/KIT_PIN` and `factory/QUALIFICATION.json`, plus added ordinary
  regular blobs at exact
  `factory/migrations/inflight-release/<40-hex>.json`.
- Require the retained control blobs to equal the receipt-bound protected
  base, the refresh receipt/topology to validate, and the effective Reviewer
  plus later Narrator heads to belong to the surviving ticket lineage.
- Every boundary that decides whether review evidence survives a
  protected-base refresh—`next-stage`, ticket publication, and bundle
  attestation—must reuse the same semantic classifier and topology validator.
  Do not maintain independent path allowlists at those boundaries. The
  qualification reducer validates the resulting authenticated receipts and
  evidence it owns.
- Application code, tests, contracts, CI, configuration, deletion, rename,
  type change, malformed receipt, discarded lineage, and every unknown path
  still invalidate the exact affected review evidence. Never broadly ignore
  `factory/**`.
- Reviewer verdict normalization may accept only proven equivalent shapes:
  standalone verdicts, exact verdict headings, exact wrapped repair owners,
  and known callback concatenation when every verdict and owner signal agrees.
  Ambiguous, contradictory, or ownerless output remains invalid.
- Validate the normalized verdict and explicit repair owner before recording
  Reviewer terminal success. A malformed result is an accounted attempt, not
  completed-role evidence.
- Invalid Reviewer formatting reruns only Reviewer within budget. Reviewer
  findings route only to the explicit Test-author or Builder repair owner,
  followed by fresh Reviewer and downstream Narrator as required.

### Checkpoints, cancellation, and rewrites

- A terminal ticket checkpoint must be exportable while sibling actions remain
  live. Never require a generation-wide or multi-ticket-lane drain.
- Controller restart after export must authenticate the exact run, role,
  charge, evidence, and consumed receipt, then finish without re-exporting or
  replaying.
- Cancellation must reconcile the process group, manifest, charge, claim,
  lease, passport, controller state, and worktree/diagnostic ref. Do not guess
  that a process or claim is stale.
- A role's required non-fast-forward rewrite is not permission for the
  controller to force-push. It requires one exact protected operator
  authorization bound to repository, Factory release, ticket/branch/state,
  signed passport, old/new heads, unchanged route, consumed repair receipt,
  typed push failure, and permitted tree delta.
- After the operator publishes that exact head, recovery must verify the
  remote tip and reopen only the invalidated role. Previous manual
  remote-tracking synchronization was an operational sharp edge; investigate
  a safe deterministic refresh, but never weaken exact-head attestation or
  automate force-push authority.
- Failed attempts retain their immutable charges but never become
  successful-role evidence.

### Budget

- Immutable manifests and authenticated passport charge history are
  authoritative. Never delete or edit prior charges.
- Budget exhaustion produces `AWAIT_BUDGET`, preserving the ticket passport,
  cell portability, and completed roles.
- An authorized cap increase uses immutable authenticated supersession. It
  reopens only that ticket from retained state and never returns through fresh
  admission.
- Every budget boundary uses the same canonical reducer semantics over the
  exact base envelope and authenticated supersession chain. Revalidation is
  required at prescribed boundaries; mutable cached results and divergent
  reducers are forbidden.
- Keep provider reservation and unknown-cost accounting conservative.
- Do not use attempt, Reviewer-round, or wall-clock ceilings as delivery stops.
  Still report time, attempts, role records, malformed outputs, and spend as
  efficiency signals.

### Publication

- Allow four open PRs to validate concurrently.
- Permit one renewable short-lived merge lease per product.
- Keep PR checks concurrent, but serialize protected-base refresh, fetch/ref
  updates, closeout, and all other shared-Git-common-directory mutations under
  the brief per-product Git guard.
- Treat the exact certified remote tip and proven ancestry as authoritative;
  GitHub `mergeStateStatus` alone is not staleness evidence.
- Before queue ordering, validate every queue record against its current exact
  head and withdraw stale records. Withdraw a ticket immediately whenever it
  leaves merge-ready state, whether or not it holds the lease.
- During qualification, clean excluded claims' publication state before
  filtering the selected cohort.
- Order currently merge-ready work by ticket priority,
  `publication_ready_at`, then ticket ID.
- Refresh against exact protected main and rerun required checks before lease
  acquisition.
- If the lease holder needs repair or is no longer merge-ready, disable its
  auto-merge where applicable, release the lease, withdraw its stale queue
  record, route only its invalidated role, and return it to the queue tail.
- One read-only transport failure may retry the identical exact-head
  observation once. A second failure or semantic mismatch fails closed.
- Rerun a failed GitHub Actions job once on the unchanged head only when
  policy, security, test-immutability, and control-plane checks are green and
  the failure is classified as an application-test transient.
- Never automatically rerun policy, security, configuration, CI-control, or
  test-immutability failures. A second application-test failure on the same
  head routes to the exact owning role.
- Do not add a database, queue service, dashboard, native GitHub merge queue,
  or second scheduler to solve publication ordering.

## Known inefficiencies and remaining questions

Confirm each against current code and evidence before proposing work:

1. **Measured outcome—improve:** T-168 and T-169 required 11 and 15 completed
   role records. Qualification remained correct, but nine rejected Reviewer
   outputs consumed $18 and the $86 cohort spend shows substantial recovery
   churn.
2. **Implemented—do not regress:** T-166/T-168 produced nine rejected
   Reviewer outputs that all contained the same explicit
   `REQUEST CHANGES / test-author` semantics. The strict deterministic parser
   fix must remain.
3. **Implemented—do not regress:** protected-base refresh originally replayed
   review for control-only Factory metadata. The narrow shared semantic
   classifier fixed that without ignoring application, test, or unknown
   changes.
4. **Observed—open:** a protected-base semantic change can make a previously
   valid fixture contract conflict with a newly merged sibling. The smallest
   likely follow-up is deterministic preflight at that exact boundary, not
   another provider role.
5. **Operational follow-up:** exceptional authorized force-push recovery
   required manual synchronization of a stale remote-tracking ref before exact
   attestation.
6. **Low-priority observability debt:** terminal passports may retain confusing
   display fields such as an old `current_state` or `current_stage` even when
   authenticated publication and reducer truth show Done.
7. **Operational follow-up:** terminal passport migration to the qualified
   successor was authenticated and replay-safe but initiated manually rather
   than by the controller.
8. **Low-priority compatibility cleanup:** `dispatch-plan` is non-agentic and
   deterministic, but remains a separate compatibility implementation rather
   than the smallest literal reconciliation alias.
9. **Qualification coverage:** the last proof used an operator-authorized
   three-ticket closeout. A future concurrency claim should prefer four live
   tickets and four overlapping provider intervals.
10. **Qualification limitation:** the successor was proven by migration,
    reducer, and protected CI but did not execute an entirely new provider
    role under that exact final SHA.
11. **Post-qualification gate, not throughput backlog:** Relay activation,
    rollback, and recutover remained outstanding before nysa-app activation.
12. **P0 observed—open:** current role transitions call `next-stage` before
    materialization and again for drift detection, while architecture text
    still claims one call. Either retain and document the identical
    fail-closed verification or replace it with one tested resolution; never
    let planning and execution choose separate stages.
13. **P0 evidence gap—open:** the current qualification reducer proves the
    four-ticket restart boundary, relocation, serialized publication, and
    concurrent PR existence, but does not ingest provider GO/terminal
    intervals or prove maximum provider overlap equals four.
14. **P0 publication defect—verify current code:** approval can acquire a
    publication lease on the pre-approval head, then commit/push a different
    approval head before requesting auto-merge. The safer flow materializes
    approval, waits for exact-approved-head checks, acquires a lease bound to
    that head, revalidates head/base/checks, and only then requests auto-merge.
    Release the lease on drift, wait, or repair.
15. **Implemented—do not regress:** the installed `factory-launch` is part of
    the exact release tuple. Certification and activation must reject a stable
    launcher whose bytes differ from the sealed candidate; bootstrap it only
    while provider/controller work is drained and retain the prior executable
    for rollback.

Do not fix all of these mechanically. Rank them by measured provider cost,
wall-clock delay, sibling blocking, manual intervention, and trust risk. Choose
the smallest shared root cause that materially improves four-ticket delivery.
Prefer deleting duplicate routing logic or reusing an existing authenticated
boundary over adding a new abstraction.

## Improvement-log discipline

In `AUDIT`, propose the exact existing-entry update or new entry without
editing it. In `IMPLEMENT`, update
`docs/evidence/software-factory-improvement-log.md` once the root cause and
immutable evidence are known; do not create one entry per symptom.

- Search for the root cause before adding an entry.
- Append a new occurrence to an existing entry instead of duplicating it.
- Add a new `FI-YYYYMMDD-NNN` only for a distinct systemic failure,
  backward transition after Spec PASS, sibling block, or
  trust/accounting/cancellation divergence.
- Record `Status`, `Area`, `Owner`, `First seen`, measured `Impact`, immutable
  `Evidence`, `Root cause`, `Smallest change`, and `Validation`.
- Link exact ticket, run/manifest, receipt/passport digest, controller event,
  PR head/merge SHA, and GitHub job when available.
- Record roles replayed or preserved, attempts, rejected outputs, charges,
  waiting time, maximum provider overlap, queue/lease behavior, and manual
  interventions.
- Do not paste raw transcripts, provider chatter, credentials, or secret
  values.
- Mark an item implemented after its focused regression. Close it only after
  one real Relay canary; then promote the stable rule to `context/memory.md`
  and the relevant architecture/runbook.

## Change and candidate rules

Begin with a read-only audit:

1. Confirm the affected repositories and read their `AGENTS.md`.
2. Verify worktree cleanliness, current branch, exact head/tree, protected
   main, PR state, and current qualification report.
3. Determine from authenticated lock, manifest, claim, and controller evidence
   whether any provider action is live. Do not use a readiness probe for this.
4. Reconcile immutable passports, manifests, charges, concurrency events,
   publication events, PR heads, checks, merges, and protected main read-only.
5. State which observations are facts, inferences, historical context, and
   unresolved questions.

If a bounded provider action is live, report that boundary and wait for its
authenticated terminal event. Do only offline, non-mutating evidence analysis
meanwhile.

Keep a frozen candidate unchanged unless immutable evidence proves a Factory
defect that must be repaired. If a Factory change is necessary:

- if any provider/controller action is live, stop at the audit boundary and
  request exact authority for the drain; never pause, reconcile, restart, or
  cancel work merely to make an improvement branch convenient;
- preserve every passport, manifest, charge, receipt, branch, and event;
- create a separate short-lived improvement branch/worktree;
- fix the shared root cause in the fewest files;
- add one focused regression;
- run only focused verification;
- let protected GitHub CI own the full regression;
- declare and freeze a successor explicitly;
- start a new qualification generation only because executable Factory
  semantics changed;
- never relabel historical role evidence as successor-SHA execution.

Documentation-only analysis does not justify altering or replacing the frozen
candidate. Never hand-edit controller state, passports, manifests, ledgers,
receipts, activation journals, or qualification results.

## Local verification rules

Do not run or repeatedly rerun:

- `bash ci/test-all.sh` without a changed/defer mode;
- the monolithic `ci/test-factory-scripts.sh` suite;
- broad local product CI;
- a full Hermes suite;
- local AI review;
- pixel-perfect/browser screenshot gates;
- ad-hoc model readiness probes.

Do not weaken assertions, timeouts, protection rules, or fail-closed behavior
to make a local suite pass.

Run the smallest checks covering the changed boundary. Examples include:

```bash
python3 ci/state-machine-test.py
python3 ci/ticket-passport-test.py
python3 ci/factory-controller-test.py
python3 ci/qualification-reducer-test.py
python3 ci/cursor-stream-test.py
python3 ci/ticket-attest-test.py
python3 ci/ticket-pr-test.py
bash -n <changed-shell-files>
git diff --check
```

Choose only the relevant subset. When preparing a PR, use the repository's
managed changed-or-defer readiness plus repository/secret checks as required
by `AGENTS.md`; do not convert a deferred full regression into a broad local
run. Protected GitHub CI owns the complete regression. Inspect every protected
CI failure exactly and never dismiss it as timing-related.

## Relay qualification for a new candidate

Do not use nysa-app or change Nysa application code. Use Relay and preserve its
history. Freeze one exact Factory candidate before the first canary role.

Prefer four small independent Relay tickets that exercise real application
boundaries. Configure capacity four, four concurrent PR validations, one merge
lease, explicit per-ticket/per-run caps, and an overall qualification
envelope.

Every concurrency qualification must prove live:

- four independent admitted claims unless an exact waiver applies;
- four genuinely overlapping authenticated provider intervals;
- concurrent exact-head PR validation;
- one-at-a-time merge publication without sibling blocking;
- no successful-role replay or double charge;
- exact reducer agreement with passports, manifests, charges, concurrency
  events, PR heads, protected checks, merge SHAs, and protected main.

Four-provider overlap must come from authenticated run/attempt intervals bound
to ticket, role, run ID, manifest digest, Factory SHA, GO, terminal event, and
passport charge evidence. Extend the reducer with focused overlap/refusal
coverage before calling that proof reducer-backed; otherwise report it as an
unresolved production-readiness gap.

Prove restart recovery, relocation, budget wait/resume, publication-priority
release, transient rerun, and protected-failure refusal with focused
deterministic regressions plus compatible immutable Relay evidence. Repeat a
live fault scenario only when the candidate changes that boundary or existing
evidence is invalid. Never manufacture provider, GitHub, budget, or controller
failure merely to fill a checklist.

Only an explicit operator statement may reduce final `target_done` to three.
The current contract still requires a four-ticket restart/recovery boundary
containing the selected tickets; a fresh three-ticket-only run does not satisfy
it. No Factory candidate-tree change is allowed after the final candidate's
first canary role.
On a proven candidate defect, obtain exact drain authority, preserve evidence,
repair on a successor, and start a new generation. Use targeted typed recovery
only; do not replay siblings or successful roles.

Qualification is evidence, not publication or activation. Use separate,
non-inheriting gates:

1. `FACTORY_PROMOTE`: exact candidate SHA/tree, destination branch, Factory PR,
   and allowed push/open/merge actions only.
2. `FACTORY_SEAL`: after protected merge, bind `RELEASE_SHA` to the exact
   current canonical Factory `origin/main`; require its tree to equal the
   frozen candidate tree and require its authenticated main push run. Install
   and seal only that SHA from a canonical Factory checkout after a fresh
   fetch, never from a stale isolated-worktree tracking ref.
3. `RELAY_PR`: exact already-certified Relay commit/tree, receipt, pin,
   envelope, and migration evidence; use the exact protected merge method
   required by its reviewed migration contract, without bypass.
4. `RELAY_CUTOVER`: exact protected Relay tree/receipt and
   maintenance-drain-plan-activate actions. Each in-flight `models migrate`
   needs its own current preview hash and operator identity.
5. `RELAY_ROLLBACK`: keep maintenance, merge the exact protected previous-tree
   revert first, then run rollback only if the candidate generation committed
   and remains active.
6. `RELAY_RECUTOVER`: new protected candidate-tree PR if needed, fresh
   certification and plan, and a new cutover approval. Never reuse a consumed
   receipt, stale plan, migration preview, or prior approval.
7. `NYSA_ACTIVATE`: later Factory-control-only Nysa
   pin/reconciliation/certification/cutover authorization. No Factory or Relay
   gate implies it.

Do not manually deploy either product.

## Required session output

Return a definitive, evidence-backed report containing:

1. current verified repository, candidate, qualification, and GitHub facts;
   persisted controller/provider facts when safely available, otherwise
   explicitly `not inspected` or `unknown` without probing;
2. whether all runtime ticket transitions still originate from the sole
   deterministic state machine;
3. measured throughput/cost/replay/publication findings;
4. the three highest-impact improvement candidates, ranked;
5. the one smallest recommended improvement and why it dominates the others;
6. exact files and focused tests it would touch in `AUDIT`, or did touch in
   authorized `IMPLEMENT`;
7. risks, fail-closed invariants, and rollback;
8. improvement-log entries proposed, and entries actually changed only in
   authorized `IMPLEMENT`;
9. qualification required after the change;
10. the exact operator authorization needed for any external or irreversible
    next action.

Ask only questions whose answers cannot be recovered from repository or
immutable runtime evidence and that would materially change the solution. Do
not make promotion, activation, force-push, cap-change, provider, controller,
push, merge, or product-mutation decisions on the operator's behalf.
````
