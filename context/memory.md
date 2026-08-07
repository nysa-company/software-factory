# Memory — software-factory decision log

Entry format: `## YYYY-MM-DD — Short title`, then `Category: Decision | Preference | System change | Context`, then 1–3 sentences. Mark superseded entries clearly and number durable decisions within this repository.

## Current truth

- Contract 1.8 emergency closeout is a narrow plan/apply authority for one
  exact already-merged ticket. It binds an open GitHub issue, explicit owner,
  bounded request window, protected-main tree/ticket state, exact PR and green
  configured checks, and either an authenticated passport plus idle blocked
  claim snapshot, an authenticated passport plus matching controller-signed
  pause, or an
  explicit operator-built/no-runtime basis. Apply requires the exact plan hash
  and reuses the ordinary ledger projection and protected closeout PR; it
  records distinct terminal evidence and never synthesizes approval history.
  A controller-signed pause may retain only idle claimed, waiting, blocked, or
  budget status; budget pauses bind the exact budget digest, and nonblocked
  lifecycle states may retain an allowed existing Resume-State overlay.
  Successor qualification binds an emergency Done receipt to that unchanged
  authenticated source passport and signed pause, retaining its roles and
  spend with zero successor provider spend rather than synthesizing an
  Approved/merged passport. Its terminal Kit-SHA may be current or an earlier
  candidate in the active qualification environment's hash-validated receipt
  chain, never an arbitrary historical release.
- Linear supports a fail-closed exact-ticket operator pull for an already
  initialized mapping. It reads only that issue, merges only operator-owned
  fields under the short map lock, survives an overlapping stale full-board
  save, and never advances full-board sync health.
- Scheduled Linear reconciliation reads one paginated issue inventory and one
  Project inventory instead of refetching every mapped object. It fetches full
  comment history only when the latest comment changes inside the approval
  window. HTTP 400/429 and GraphQL quota responses produce one typed bounded
  cooldown; no Linear call is made until its persisted expiry.
- Qualification pre-seal and dispatch both require every selected ticket's Git
  blob in the sealed control checkout to equal protected `origin/main`.
  Qualification-only metadata can therefore never validate one contract and
  dispatch another.
- Ordinary admission isolates a ticket with malformed dependency syntax and
  reports its exact ID in controller results, events, and durable incident
  evidence while eligible siblings continue. A selected qualification ticket
  with the same defect still refuses the sealed cohort globally.
- An operator-owned null initiative remains an explicit versioned tombstone,
  but a Ready ticket made initiative-less is never silently discarded:
  admission reports its exact ID and `initiative_missing` while healthy
  siblings continue.
- Linear Project creation refuses missing mapped Projects, foreign-team
  mappings, duplicate durable markers, and same-name identity conflicts;
  Doctor exposes the canonical mapped Project IDs and URLs.
- A contract-blocked Linear baseline is recorded once per substantive blocker.
  Exact resume directives and reconciler-authored writes do not advance it;
  accepted same-blocker decisions survive overlapping saves, while rejected
  moves stay visible in Linear and typed sync health.
- An active contract repair with no owner success may follow one exact
  authenticated forward passport migration after an operator preflight fix.
  The old signed record is archived, the active record is rebound without
  incrementing attempts, and any ambiguous lineage fails closed by name.
- Production certification, activation planning, and activation reject any
  `factory/QUALIFICATION.json` before receipt or journal mutation. Sealed
  qualification continues to require its exact manifest.
- Activation validates protected-main Done or lease-free Canceled truth before
  considering retained ticket refs. A stale terminal ref is left untouched for
  lane safety; genuinely nonterminal protected truth still requires the exact
  branch lease and authorization.
- Done is projected through one separate exact-ticket Linear mutation only
  after the closeout receipt validates on protected main. The issue is re-read
  as exact Done and one controller event is recorded before lease release;
  missing mapping, API failure, or changed terminal truth remains retryable.
- A fixed ticket branch may retain any number of historical closed or merged
  PRs. Publication selects exactly one current open PR, while merge detection
  and Done select the exact PR number sealed in the approval attestation;
  multiple open candidates, a missing bound PR, or branch/base/head drift
  remains fail-closed.
- Protected-terminal qualification evidence carries the authenticated
  generation and manifest digest used to select it. The reducer accepts those
  exact boundary fields while missing, partial, unknown, or cross-generation
  event shapes remain fail-closed.
- Qualification requires one current-candidate cell relocation whenever any
  selected ticket still needs publication. When every selected ticket is
  already authenticated terminal, zero or one valid relocation is accepted;
  duplicate or foreign relocation evidence still fails closed.
- Production helpers run only from installed sealed releases whose SHA is on
  Factory `origin/main` with exact successful protected CI. Qualification may
  seal a clean local SHA/tree without that reachability; trusted launchers label
  the two scopes, mutable kits cannot claim either, and run manifests preserve
  the distinction.
- Linear description comparison canonicalizes the observed serializer-only
  ordered-list indentation, continuation, renumbering, inline-code, link, and
  fence-boundary forms. Nested-list structure, fenced content, and meaningful
  edits remain significant, and Git retains the exact ticket contract.
- Ordinary route-migration previews return compact source, readiness,
  journal-tail, and approval digests. `--include-journal` is the explicit
  diagnostic path. Apply performs one fresh readiness round and requires the
  exact preview readiness digest; it does not repeat identical probes inside
  the same command.
- Every route-journal consumer accepts both legacy inline release migrations
  and compact migrations whose canonical prior-resolution digest matches the
  active history. A mismatched digest remains fail-closed before attestation.
- A product using certification plan v2 has one exact runtime tuple: Factory
  SHA/tree, product SHA/tree, Contract, Node, and npm. The shared preflight runs
  before readiness tests, qualification materialization, certification suites,
  and sealed qualification launch; receipts and activation bind the same tuple,
  while malformed, missing, unknown, or mismatched values fail closed.
- The owner bootstrap pins a v2 product's plan-matching Node/npm/npx executables
  in `~/.factory/bin`, first in the sealed launcher PATH. The operation refuses
  unsafe paths and version drift before replacement and verifies the resulting
  owner-local tuple; system-wide runtime selection is never changed.
- Isolated subscription CLI homes restore the exact sealed task PATH from their
  final zsh login hook after macOS `path_helper` initialization. The hook exits
  before a requested product command when Node or npm differs from the active
  certified tuple; provider CLIs may still use their own bundled runtimes.
- Contract 1.8 uses a non-agent, non-overlapping one-shot controller and
  one-use state-machine receipts. Tickets are branch/passport identities, not
  lane or worktree identities; four disposable cells and PR validations may
  run concurrently behind one renewable per-product publication lease.
- A resumed Contract 1.8 blocker may leave the failed receipt and role on its
  blocked controller claim after the state machine has created the signed
  owner-only repair. The controller reopens it only when both retained fields
  exactly match that repair record and the current remote passport is
  authenticated. Recovery clears only those stale controller-cache fields;
  ordinary reconciliation then invokes the state machine once to authenticate
  repair, passport, charge, terminal, dependency, and stage evidence. Recovery
  itself never resolves or substitutes a stage.
- Contract 1.8 resolves each stage exactly once. Before scheduling after a
  release change, blocked, claimed, and waiting claims authenticate and migrate
  any prior-release passport. Maintenance appearing after stage resolution
  but before submission leaves the receipt unconsumed, settles and parks the
  clean claim, and releases its lease; it never creates a provider attempt,
  charge, or successful-role replay.
- Contract 1.8 sequences a ticket with a current authenticated passport from
  that passport's ordered completed-role evidence. The state machine validates
  the exact ticket, branch, head, route, release, and HMAC before passing an
  owner-only ephemeral sequence to its single `next-stage` call. The runtime
  ledger remains the sequencing source only before a ticket has a passport;
  release migration or qualification takeover cannot erase successful roles
  from scheduling while retaining them in authenticated history.
- A durable-GO exit 125 with no submission marker, progress, or usable
  telemetry exports its full conservative reservation into the ordinary
  passport and remains blocked under the same release without invoking model
  fallback. Only a successor release may clear that exact receipt after the
  signed remote passport proves its charge was exported once; a repeat under
  the successor blocks again. New runs record
  `adapter_submission_unconfirmed` and a bounded diagnostic-output digest,
  while only the exact earlier blank-reason/empty-output shape remains readable
  during upgrade.
- A successor may recover the one historical Builder false terminal with
  `abandoned`/`abandoned_conservative`, durable GO and submission, exit 128,
  and blank role-exit/reason only when its authenticated receipt, unique
  manifest, owner-only output and terminal-success progress, charge, clean
  cell, current Factory passport, and remote head all converge. The correction
  admits only the production `cursor-openai` and `cursor-anthropic` adapters,
  and permits one correction-only all-v2 successor suffix when an authenticated
  failed-run export advanced from the receipt input to its descendant Builder
  output before migration. The final edge must bind the current passport
  parent, generic receipt lineage is unchanged, and the correction remains
  HMAC-signed, retained, idempotent, and controller-cleared; every mismatch
  stays blocked.
- Contract 1.8 preflight consumes the already-verified transition stage. Normal
  Planner work still requires the visible Planning state; an authenticated
  `FIX planner` receipt may run beneath a later coarse state without mutating
  that state backward or recomputing repair ownership.
- A qualification Planner-preflight block with no passport, run record, or
  active process may retry only from a clean remote-equal cell, one unconsumed
  Planner receipt, and a route pinned to the current Factory. The controller
  reacquires one lease, issues a fresh receipt, and reruns sealed preflight;
  failure releases the lease, while only a pass reopens ordinary execution.
- A signed completed Planner repair retains narrow catch-up authority while
  its reopened test-first epoch follows the exact alternating
  Planner/Spec-linter prefix beneath Building or Review. A verified uncapped
  Spec-linter FAIL receipt may derive launcher-only `CATCHUP planner`
  admission; ordinary, stale, malformed, reordered, or capped receipts remain
  closed, no ticket state is rewound, and no general bypass exists.
- A tests-first epoch begins only in a ticket-only commit with one higher
  frozen-contract heading and one matching PASS marker. New Planner output uses
  the canonical append-only form. Historical output may replace only the
  latest heading and matching established PASS one-for-one; every partial,
  mismatched, repeated, lower, mixed, or malformed shape remains closed.
- An accepted successful late Test-author push can be normalized only after a
  successor migrates the passport on the unchanged old head. One protected
  authorization binds both Factory identities, the exact successful run and
  charge, old/new tree-identical heads, route, and protected merge parents;
  only an explicit exact-old-head force-with-lease may publish it.
- A consumed contract-block receipt remains in lineage across protected
  history normalization only through one authenticated same-release migration
  edge with byte-identical old/new Git trees, or through one protected
  history-repair edge whose only final-tree delta is the exact append-only
  current ticket log. Current route/base bindings remain mandatory. Passport
  export and block recovery share that proof. Missing authorization, semantic
  drift, or multiple matches fail closed; this is not a general state-machine
  override.
- If the active signed backward repair itself contract-blocks, its later coarse
  state is retained as the exact resume target. The same repair authenticates
  block recovery and resume; ordinary role/state drift remains refused.
- A successor may retain that active repair after its owner commits a blocker
  only when the consumed FIX receipt, parent blocker, unique terminal manifest,
  authenticated charge, current passport stage, ancestry, and migration suffix
  all bind the repair authorization to the successor passport.
- During idempotent block recovery, the one exact receipt-bound operator commit
  may sit directly after the authenticated passport head. The state machine
  validates that ticket-only commit against the passport head, validates the
  retained repair at that authenticated boundary, and then performs the
  ordinary passport migration before resume. Any other descendant remains
  closed.
- A consumed `FIX <role>` contract blocker may retain its later coarse
  `Resume-State` before the signed repair record exists only when the exact
  receipt and authenticated blocked passport agree on that FIX stage. Lease
  recovery, block replay, and resume share this evidence; mismatched stages,
  passport state, receipts, roles, or leases remain closed. A receipt/passport
  Factory mismatch additionally requires the authenticated historical charge,
  ordered release history, exact current passport digest, and blocker Git
  lineage. Once the signed repair exists, its narrower repair-migration proof
  remains authoritative.
- Planner, Spec-linter, and Test-author independently evaluate exact generated
  fixture values from their initializer/reset. An expected identifier,
  sequence, counter, or timestamp the setup cannot produce—or a repair scope
  excluding its required setup fix—is a contract block before Builder.
- Every new committed Blocked-Escalated source clears any prior resume overlay
  and timestamp baseline before the remote state is interpreted, including
  repeated blockers with no overlay. Its first exact remote blocked observation
  records the new baseline; only a strictly later declared-state update resumes.
- Accepted state overlays bind the exact committed ticket text; any later
  ticket commit, or a legacy missing binding, clears the overlay before
  projection. Contract-block recovery asks for resume only when the committed
  ticket visibly names the exact current blocker receipt, while the state
  machine retains final directive authority.
- Contract 1.8 at ticket capacity above one requires an exact owner-approved
  subscription provider policy and activation covering every enabled Cursor,
  Claude Code, and Codex route at no less than ticket capacity. The installed
  launcher gives every call a ticket- and execution-cell-neutral owner-local
  home/config/tmp root with a private authentication copy. Doctor,
  certification, activation, and role pre-admission refuse missing or drifted
  configuration instead of silently selecting the legacy provider lock.
- Native Claude readiness uses one disposable owner-only configuration with a
  securely copied credential for version, help, OAuth, and authenticated-status
  checks. Ambient Claude settings and hooks cannot alter route readiness, and
  unsafe credentials or cleanup failures remain fail-closed.
- A successor may preserve one old-catalog Cursor Spec-linter success only when
  the authenticated output, progress journal, route mismatch, charge, exact
  ticket-only output/revert, and bounded authenticated route-migration chain
  converge.
  It reapplies the exact append on top of the migrated Kit-SHA/route without
  force; provider replay is forbidden.
- Contract 1.8 role execution retains the validated project in a non-exported
  host binding for every receipt recheck while keeping project model-state
  controls out of provider environments.
- Contract 1.8 retries one failed exact-head remote observation before
  classifying branch drift. A second transport failure or a different head
  remains fail-closed, and only the trusted wrapper pushes role commits. The
  ticket-PR boundary applies the same bounded retry to its read-only exact
  branch-head observation.
- After a verified trusted push, remote-tracking compare-and-swap treats an
  already-converged desired SHA as idempotent success, and an explicitly
  expected absence initializes the ref through Git's zero-OID compare-and-swap.
  Every third, unexpectedly missing, or unreadable state still fails closed.
- Production-successor qualification authenticates the clean activated product
  tree separately from current protected main. Protected main must contain the
  active commit, and the local qualification control worktree must be based on
  that current protected ref with only the admitted control diff. Qualification
  therefore never requires a preliminary product activation.
- A sealed qualification may register its immutable product checkout at a
  detached protected-main SHA. The runner records detached versus branch
  identity explicitly while still snapshotting exact HEAD, status, and content;
  ticket worktrees remain branch-bound.
- Sealed qualification provider admission scopes product- and ticket-budget
  accounting to the exact project plus frozen candidate SHA. Predecessor
  candidates cannot exhaust a successor's allowance, multiple roots for the
  same candidate cannot reset it, and the machine-day cap remains global.
- Contract 1.8 budget exhaustion blocks only a resolved paid `RUN` or `FIX`
  stage. Deterministic validation and receipt-bound post-role reductions still
  run, so an already successful and fully charged Narrator may reach bundle
  attestation without another provider reservation; missing or invalid
  evidence still resolves to a provider stage and remains budget-blocked.
- Reviewer terminalization normalizes exact verdict-only and `Verdict:` lines
  with bounded heading, emphasis, and terminal-period variants, exact wrapped
  repair-owner lines, and known Cursor background-callback concatenation only
  when every verdict and owner signal agrees. Ambiguous, contradictory,
  negated, prose-only, and ownerless output remains invalid.
- Contract 1.8 treats GitHub's exact empty no-required-checks-yet response as
  publication wait. Every other malformed or non-JSON check response remains
  a fail-closed controller error.
- Contract 1.8 runs deterministic provider-free preflight once on the Planner
  receipt after entering Planning. Later roles resume from authenticated
  evidence without repeating kickoff preflight.
- Contract 1.8 resolves every dependency before paid admission. A legacy
  Backlog ticket whose protected application PR already merged may satisfy only
  dependency readiness through one manual, no-bypass, atomic protected
  fulfillment batch bound to the exact PR/check/basis/source-ticket evidence
  and target Factory SHA. It is not terminal truth and never marks the ticket
  Done; malformed terminal evidence cannot fall through to this path. The
  atomic commit may additionally contain only the exact same-target in-flight
  release authorization.
- A resolved Contract 1.8 dependency refresh may recover only a regular
  both-modified conflict wholly inside protected-main `TEST_PATHS`. The host
  binds all three conflict blobs and the exact two-parent merge, retains the
  protected test as the baseline, and the state machine issues one
  HMAC-bound `FIX test-author` checkpoint. Earlier role evidence and charges
  remain valid. Sibling merges may advance protected main while this exact
  checkpoint runs; normal refresh absorbs that base afterward. Retirement
  requires the consumed FIX receipt, exact repair head, authenticated
  terminal passport, matching evidence/charge pair, and only regular
  modifications to the listed tests or ticket log. Every application, mixed,
  control, configuration, CI, contract, rename, add/delete, non-regular,
  missing-receipt, or unknown conflict fails closed.
- During Contract 1.8 qualification, the first terminal failed Cursor attempt
  retains the ticket claim, converts an initial v1 route plan into a
  same-release v2 journal when needed, and appends the existing same-family
  direct-CLI fallback. The fallback recovers idempotently after controller
  restart; a second task-submitted attempt for the same role is the no-progress
  stop.
- Attempt-cancellation plan and apply calls derive one stable preview from the
  exact manifest, PID record, and reason. Snapshot drift still changes the hash
  and refuses cancellation.
- Contract 1.8 may reconcile a retained zero-provider ticket branch only from
  a protected-main exact-head authorization. The old branch must contain only
  canonical pin/Planning controls and an unchanged ticket contract; recovery
  preserves history through a non-force protected-main merge.
- Contract 1.8 qualification closes an acyclic ordered three-ticket cohort at
  capacity three or four independent tickets at capacity four with $100 total,
  authenticated ticket-cap overrides, and $2 per run. External dependencies
  require protected terminal evidence and excluded claims remain parked. Both
  forms require passport-bound Factory history, no duplicate successful
  role/head or charge, exact restart and relocation, a frozen final candidate,
  protected checks, exact merged heads, and protected-main Done. Only the
  four-ticket form claims concurrent PR validation; an earlier fresh
  three-ticket run may retain its authenticated four-ticket boundary.
- Contract 1.8 production-successor qualification binds the installed source
  Factory SHA and reuses canonical controller passports, route state, provider
  accounting, and reconciliation lock in place. It does not copy or re-sign
  live state. The reducer preserves cumulative history while capping and
  reporting new candidate spend at $10 per run, $100 per ticket, and $300 for
  the three-ticket cohort. Each takeover permits one frozen candidate; a
  replacement root accepts only a unique contiguous authenticated v2 release
  suffix from the unchanged installed source through any prior qualification
  candidate to the new final candidate.
- A protected terminal's Done-approved PR head may precede its current passport
  head after signed post-merge route migrations. Successor adoption and final
  reduction accept it only through one unique contiguous v2 suffix ending at
  the current passport Factory, head, base, route, and parent digests;
  historical membership, disconnection, ambiguity, and parent substitution
  remain closed.
- A deterministic `REFUSE` receipt never migrates the ticket passport. The
  controller first blocks the claim, then owns authenticated cross-release
  passport migration and the durable pre-route pending marker on its next
  one-shot.
- Contract 1.8 ticket-PR validation preserves Reviewer/Narrator evidence across
  a control-only refresh only when the receipt-authorized stage, committed
  direct-after-merge refresh topology, shared semantic classifier, and exact
  retained protected-base blobs all agree. The latest effective Reviewer and
  later Narrator must also belong to the receipt-bound old head; earlier
  discarded-lineage rows remain auditable but do not disqualify a later valid
  pair. Unknown and semantic changes still invalidate review.
- Ticket-PR publication and bundle attestation use one shared fail-closed
  classifier for post-review Narrator screenshots. It admits at most 32
  current-ticket regular PNG blobs of at most 2 MB whose complete chunk stream
  is valid and whose paths are referenced by the current bundle (or by the
  reviewed bundle for deletions). Unreferenced, nested, sibling, executable,
  malformed, and excessive evidence remains implementation drift at both
  boundaries.
- Contract 1.8 evidence bundles are scoped to the latest effective, non-void
  Reviewer generation. Only successful Narrators after that Reviewer may
  decide the bundle boundary: an unchanged generation preserves an explicit
  `NOT APPROVABLE` result and routes to repair, while a later approved Reviewer
  makes the older bundle and attestation stale and authorizes one fresh
  Narrator. A rejected latest review cannot inherit an older approval, and
  planning/build gates do not reduce later-stage generation evidence early.
- Contract 1.8 passport export accepts a terminal receipt across one uniquely
  matching contiguous authenticated migration suffix. Every new versioned
  edge retains the raw and embedded digests of its authenticated source
  passport; the first edge must name the exact passport file bound by the
  receipt. One pre-v2 snapshot may cross one new edge only through a one-file
  protected-main authorization binding the exact receipt, source passport and
  history, target identity, and terminal accounting. Its commit adds only that
  record; export requires protected main to remain the signed endpoint, or an
  additional authenticated base edge. Arbitrary ancestors, broken chains,
  reused bridges, and unknown changes remain outside lineage.
- Contract 1.8 publication queue membership follows the current deterministic
  transition rather than historical readiness. A ticket that is no longer
  merge-ready withdraws its stale queue record, while an active publication
  lease remains removable only through its capability-bound release.
  Qualification startup also withdraws queue state for excluded parked claims
  without resuming their ticket lifecycle.
- Protected terminal validation permits a later byte-identical reintroduction
  of a normal Done receipt after rollback only when exactly one original
  direct-child closeout still matches its authenticated parent and current
  blob. Changed or ambiguous evidence remains invalid.
- Fresh development lanes and trusted ticket-state reconciliation omit the
  Reviewer checkpoint argument when no authenticated import exists, including
  on macOS Bash 3.2. Cancellation recovery accepts authenticated shell status
  130 and macOS SIGTERM status 143, retaining interrupted output on a
  diagnostic ref before restoring the trusted ticket head.
- Explicit contract-repair stages for Planner, Spec-linter, Test-author, and
  Builder map only to their named role. Ambiguous or unsupported repair text
  remains non-runnable.
- Cross-release contract repair accepts a historical consumed receipt only
  through the current authenticated passport's ordered release history, exact
  charge/manifest binding, current branch ancestry, and absence of successful
  evidence for that receipt. Its current exact-ticket lease must also be live.
  A successor controller may reconstruct fields cleared by an earlier upgrade
  only from that same latest terminal boundary.
- A signed contract-repair owner survives an intervening dependency wait and
  later Factory migration only when its bound passport uniquely begins a
  contiguous authenticated v2 migration suffix ending at the current passport.
  The original blocker charge must remain unique and absent from successful
  evidence; otherwise the state machine fails closed.
- Portable Spec-lint evidence is compared semantically after normalizing only
  Markdown indentation; export and replay use the same exact marker grammar.
- Selected-ticket publication does not require consuming the compatibility
  batch approval. It still requires terminal role evidence, exact drain,
  reviewed head, clean branch, and the consumed ticket-scoped approval.

- Fresh Contract 1.7 product planning removes stale canonical Spec-lint,
  Reviewer, and repair-owner control lines while preserving historical prose
  and quoted signed-review detail. Source tickets may retain prior lifecycle
  evidence without confusing the new role sequencer.

- This repository is a product-agnostic factory kit; product repositories carry only their factory state and CI integration.
- Durable decisions in this repository use their own numbering, beginning at Decision 1.
- Model routing is portfolio policy: the catalog separates transport, gateway, inference provider, family, account route, selection ID, and reported identity; profiles order all-six-role portfolios with distinct production/checking families. Routes are pinned at the ticket boundary and may change mid-ticket only through the Contract 1.4 one-use Linear-approved journal flow; one logical role attempt submits to at most one process.
- Stable product and operating truth lives under `docs/`; executable prompts, copied templates, conformance evidence, and vendored material remain colocated with their consumers.
- The repository adopts Nysa Agents baseline v3 as a toolkit with repository, secret, artifact, Git-flow, CI, and config-review enabled. `bash ci/test-all.sh` remains the unconditional full command; managed local readiness uses `bash ci/test-all.sh --changed-or-defer origin/main HEAD`.
- Dynamic CI selection is fail-closed and evidence-gated: six audited leaf components remain available for focused local and pull-request work. Broad readiness and pull requests run policy gates and defer complete coverage; every `main` commit runs the complete registry in four isolated hosted-runner groups per platform while retaining the six established release-evidence aliases. The public factory-script suite runs six fixed internal workers with isolated temporary roots while keeping launch-, cancellation-, Git-, accounting-, and cleanup-coupled cases sequential within a worker; failure and interruption drain each worker process group.
- Live products resolve sealed exact-SHA kit releases under `~/.factory/kits` through the stable `~/.factory/bin/factory-launch` contract; kit merges are candidates until a product-specific certified activation.
- The stable installed launcher is part of that exact release tuple.
  Certification and every receipt validation require its bytes to match the
  sealed candidate; a changed launcher is bootstrapped only while controller
  and provider work are drained, with the prior executable retained for
  rollback.
- Install records owner-only, expiring kit-suite evidence for the exact sealed release. Exact protected-main GitHub Actions full-suite evidence is mandatory and is followed by a sandboxed platform smoke; missing evidence fails closed without a local full fallback. Certification reuses evidence only when every release, physical-tree, host, platform, suite-definition, tool-version, source, and configured-lifetime binding matches; product certification and binding checks always rerun.
- Product certification stages a checksum-verified pinned scanner into the disposable product copy before entering its network-denied sandbox, so managed secret scanning never depends on a prewarmed product cache or certification-time network access.
- Certification phase reuse is explicit and artifact-only. Same-root restarts
  retain self-hashed local evidence. Across commands, the Factory verifies an
  owner-only HMAC entry outside the product sandbox, stages it read-only, and
  the runner rehashes and restores only the complete plan-declared regular-file
  manifest. Exact Factory/product trees, plan, dependencies, runtime, command,
  runner, network, TTL, size, mode, and path bindings are mandatory; raw logs
  and undeclared/test/policy/security/configuration side effects never persist.
- External products require one full `factory/KIT_PIN`, and the first role launch records a durable ticket `Kit-SHA`; only the in-repository conformance test bed has an implicit runtime pin.
- Release activation is maintenance-gated, receipt-bound, and journaled. Failed-cutover recovery keeps `MAINTENANCE`, stops product factory services, reconciles any interrupted transaction, restores the protected previous pin/tree, and calls rollback only for a committed active candidate; automatic pruning is intentionally unavailable.
- The required aggregate `ci` status always reports. Pull requests retain policy and applicable targeted checks; every merged SHA runs complete Linux and macOS verification before it can become a release. Relay generation 4 runs documentation-only release `35c2e10` with healthy generation 3 on `3b63cc7` retained as its exact current-tree rollback baseline; the five-minute outage target and formal rollback RTO remain unaccepted.
- A composite Factory successor may batch only independently green, already
  authorized issue commits from isolated branches. It starts from the exact
  protected base, preserves issue/source/candidate commit provenance, passes
  combined regressions and managed readiness, and pays one protected-main full
  CI and sealed installation/certification cycle. Textually or semantically
  overlapping, order-dependent, trust-boundary-sharing, or result-changing
  changes remain separate.
- On macOS hosts where `/usr/bin/python3` is an xcrun shim, the launcher and release sandboxes use the fixed Command Line Tools Python binary when available; this preserves default-deny Seatbelt behavior without xcrun cache writes outside the sandbox.
- The no-record default is `cursor-balanced-v2`: it preserves `balanced-v2` models and effort levels while trying the matching Cursor route before native Codex/Claude CLI. Explicit active profiles and committed ticket route plans remain authoritative; `balanced-v2`, `legacy-balanced-v1`, and the earlier priority profiles remain available for compatibility.
- Cursor adapters append a trusted execution requirement after role and task text so factory roles stay in the default agent execution mode instead of switching to Cursor Plan or Ask mode.
- Parallel kit branches, worktrees, PRs, and inert candidate releases are supported. Product activation/rollback remains serialized; contracts 1.1 through 1.5 default to one ticket and permit at most four exact-worktree ticket leases, while Contract 1.6 defaults to four and permits at most six. `MAX_CONCURRENT_TICKETS` is the single coupled worktree/provider capacity setting. Contract 1.6 may bypass the product-wide provider lock only for an exact activation-gated API route executed through the isolated runtime, broker, networkless worker, and trusted artifact controller. Contract 1.8 multi-ticket subscription routes use exact owner-activated transactional admission; older contracts and capacity one retain legacy serialization.
- Spec-linter and Reviewer escalation overrides accept only an exact authorization for the next semantic round. Test immutability treats `.gitignore` and `context/memory.md` as exact-file bookkeeping exemptions, while documentation remains contract-significant; revert branches use `chore/<slug>-revert`.
- The sole emergency state-machine recovery mechanism is the exact current-
  receipt-bound `OPERATOR RESUME` ticket commit. No generic lifecycle bypass is
  approved; envelope and semantic-round overrides grant no state authority.
  Any future exception requires exact owner authorization and a dedicated
  GitHub issue before implementation or use.
- Ticket execution reads Git-authored state from the exact ticket worktree/committed branch and overlays Linear-owned fields from ignored `factory/linear-map.json`. Mutating roles must commit cleanly and retain the role-input commit as an ancestor; the trusted wrapper quarantines a non-Test-author rewrite, restores the authenticated input without touching the remote, and otherwise non-force pushes and verifies output. Reviewer must leave Git unchanged, and Test-author ancestry repair remains separately operator-authorized.
- Trusted ticket and role pushes use only the exact product origin bound by the active certification receipt. Contract 1.2 still stops in Review. Contracts 1.3 and 1.4 provide trusted bundle, exact newer Linear approval/protected auto-merge, and merge/deployment/Done closeout attestations while generic ticket-state keeps refusing evidence-sensitive transitions.
- Runtime costs are authoritative in atomic run manifests and materialized into ignored `factory/runtime-ledger.csv`; tracked `factory/ledger.csv` changes only through deterministic close-out projection, which refuses every active or ambiguous claim and `factory/runs/*.pid` record.
- Backward-compatible ledger reduction collapses a legacy durable reservation followed by its identity-matching terminal row; every other conflicting duplicate run ID fails closed.
- Product and machine runtime configuration is parsed as whitelisted data, never sourced as shell. Budget values are positive and coherent, and an explicit global-ledger path must be absolute before any probe, manifest, or task.
- Provider output and same-UID filesystem state are untrusted: durable GO precedes the adapter gate, the runs root and records are opened without following replacement links, output is captured on a wrapper-held descriptor, and only bounded adapter telemetry is consumed, with full-reservation fallback. A product-level control lock serializes provider intervals; any new or changed sibling manifest, persistent claim, owned manifest, global-ledger, or registered-checkout mutation fails the role. Hostile same-UID prevention requires OS isolation; the portable wrapper promises detection, conservative accounting, and no advancement instead.
- Hermes contract 1.6 changes its omitted capacity default to four and expands its active bound to six while preserving active 1.0–1.5 defaults and bounds.
- Fresh ticket worktrees are created from protected main and pass through trusted materialization before preflight so their exact remote branch exists. Linear Project removal is represented explicitly in the ignored overlay and clears the effective initiative until reassignment.
- Contract 1.6 ticket PRs wait on required exact-head GitHub checks without launching a role, expose completed failures to Reviewer, and revalidate successful checks plus Reviewer lineage before Narrator. Any later Builder or Test-author run forces a fresh Reviewer.
- The stable launcher executes the Contract 1.6 `ticket-pr.py` helper with its fixed isolated Python interpreter, never the shell-only helper path; launcher-level contract coverage guards this boundary.
- `ticket-pr.py` forwards the launcher's canonical dispatcher lease into its internal sequencer check. A missing PR may be recovered at Narrator only after the successful Reviewer lineage proves that no implementation changed after review.
- Reviewer lineage is execution-bound, not billing-bound: a manifest requires `phase=completed`, `exit_status=0`, and `role_exit=ok`; `accounting_state=abandoned_conservative` is accepted only alongside those fields because Cursor CLI runs reserve the full budget when exact cost is unavailable.
- Narrator recovery permits post-review changes only to the current ticket document and its exact route journal. The sealed model manager independently validates that route journal before GitHub access; product, test, sibling-ticket, and all other path changes invalidate review.
- An unattested evidence bundle missing required sections or its approval question authorizes exactly one additional Narrator run; another invalid result escalates instead of reaching operator approval.
- Post-review route changes must be append-only release-migration revisions validated by the sealed model manager. Bundle attestation accepts that exact route path, replays each revision's kit and resolution, and binds every successful run to its historical route blob.
- Activation, reconciliation, and rollback validate nonterminal `Kit-SHA` affinity from committed exact ticket branches. Plain configuration clears its full allowlist before optional file loading, so inherited environment values cannot become machine policy.
- Protected in-flight release authorization accepts exact old-kit ticket heads from Ready through Approved. A v1 plan becomes a v2 journal; an existing v2 journal receives a parent-hashed release-migration revision that preserves its full history and active resolution. Neither path advances ticket state.
- Contracts 1.2 through 1.6 reject dirty exact ticket worktrees before ordinary ticket helpers. Contract 1.2 treats approval overlays as unsupported stops; contracts 1.3 through 1.6 consume merge approval only through an unchanged evidence-bound approval attestation.
- Operator overlays may materialize only kickoff and declared non-sensitive resume state changes; factory phases remain transition-owned. Git-backed Linear projection uses exact ticket refs then committed HEAD, never uncommitted checkout content.
- Canceled is a terminal non-execution ticket state for operator-withdrawn or superseded Backlog work. Linear may authorize Backlog to Canceled; the durable ticket records the reason and replacement without manufacturing run or approval evidence.
- Dispatcher lease renewal uses only the short dispatcher-state lock. It validates exact ownership and checks KILL/MAINTENANCE before and after mutation, so a provider holding the launch lock cannot starve an ordinary heartbeat while control-plane stops remain fail-closed.
- Provider-lock owners are bound to wrapper PID, process start, and a private token. Ordinary launch debounces transient owner-liveness misses but never reclaims stale or unsafe ownership; normal release atomically renames an owned lock before cleanup, and only the kill switch may quarantine a provably stale unchanged lock after KILL publication and recorded-process drain.
- Open-source factory frameworks remain references, not replacement control planes: any adopted execution or sandbox component stays behind `factory-launch`, while sequencing, budgets, role separation, Git authority, evidence, and operator approval remain factory-owned. The first justified experiment is a pinned SWE-ReX local-container backend for one non-production role; E2B or Daytona becomes relevant only if that canary proves local isolation insufficient.
- The operator activates model profiles by exact preview hash and may add narrow TTL-bound `credits_exhausted` overrides; subscription quota telemetry is incomplete. Ticket pinning commits and pushes Kit-SHA plus the exact six-role plan atomically. Post-submission retry remains forbidden; an eligible failed GO attempt may instead create one authenticated append-only fallback revision.
- Kimi K2.6 is disabled experimental through Claude CLI/OpenRouter/Moonshot, appears in no profile, and has not had a live or billed pilot. Credential rotation is required before a pilot, and direct same-UID token exposure remains without a broker or OS isolation.
- Route-journal provenance can support future provider/family/model budgets, but none are implemented and the ledger schema is unchanged. Model management, fallback, and evidence-bound ticket attestations are integrated under Hermes contract 1.4.
- The Contract 1.3 cutover has two independent one-time formats: legacy-closeout for the exact authorized Contract 1.2 batch and terminal-backfill for the exact authorized pre-contract terminal-Done batch. Both are separate from normal attestations and route plans, become authoritative only through one manual protected product merge, and use the same fail-closed protected-main terminal reader; plain Done never suffices.
- Protected-merge reconciliation is a separate migration-only complete-batch adoption path for already-merged product changes whose old evidence cannot be refreshed safely. Its generated authorization, exact receipts, Done/Migration projections, target pin, and companions are bound atomically by one manual protected product merge. The reconciliation evidence and terminal projections remain immutable afterward; later authorized releases may evolve `KIT_PIN` and companion paths without invalidating that historical proof.
- T-013 through T-016 alone use the audited aggregate-check legacy class because their PRs predate separate policy/app-test jobs; every other reviewed legacy ticket still requires all four authentic app-bound checks.
- The isolated process-group wrapper may wait up to two minutes for the trusted controller's final pre-submission acknowledgement. No adapter starts before that gate, so expensive protected-history validation can finish without weakening kill-switch or orphan prevention.
- Factory development may use the macOS-only disposable lane against an exact clean commit, a synthetic product, and a local Git remote. Mock mode is network-denied under outer Seatbelt and must finish below 15 minutes; real Cursor uses the authenticated CLI session, Cursor's explicit internal sandbox, and a one-use executable/session-bound approval because the current CLI cannot authenticate inside nested Seatbelt. Only Cursor subprocesses receive the session home, and Cursor's hardcoded temporary root is redirected into the lane for each invocation. Lane artifacts have no release receipts or activation records, and its local product scheduler is not production-orchestration evidence. The sealed Contract 1.8 qualification environment is the pre-promotion proof for the exact production launcher, controller, passport, recovery, and release-upgrade paths.
- A legacy serialized development Cursor lane may reclaim its hardcoded temporary bridge only when it is an empty owner-owned directory tree with no group/other write permission and all subscription providers are idle. Files, symlinks, content, unsafe ownership or modes, and active providers refuse; the replacement remains an atomic lane-target symlink with exact-owner cleanup. Contract 1.7 product Cursor attempts use isolated owner-only home, configuration, data, temporary, and credential roots, so unrelated legacy bridge state does not affect their admission.
- Subscription and product development lanes copy CLI session files once, then bind readiness, version evidence, approval hashes, and role execution to the same sanitized lane-local session environment and working directory. Ambient authentication variables, caller working directories, and external Cursor session state cannot satisfy readiness; unavailable copied authentication stops before approval consumption, lease claim, reservation, or task submission.
- Development-lane subscription readiness retries three times with a one-second delay between misses. This absorbs short lane-local CLI session transitions while preserving the same fail-closed pre-approval boundary.
- Development interactive subscription authorization is bounded to five minutes. A native Claude access token with less than five minutes remaining is unavailable before task submission, so the existing profile resolver may select the authenticated Anthropic-family Cursor route under its cap.
- A fresh development product lane derives its lane-local machine-day cap from
  the same validated isolated envelope that owns its ticket/day caps; seeded
  lanes retain their cumulative accounting override and host production caps
  remain untouched.
- Retained-product resumes stabilize lane-local subscription readiness before planning and before execution validation. The internal run reuses that execution proof, while each pinned role still verifies its exact route before reservation or task submission.
- Development planning issues one approval per ticket. The deterministic
  controller uses `product-ticket-run` and `product-ticket-resume-plan`, so a
  failed ticket resolves and resumes its exact mechanical stage without
  requiring sibling drain or hash-binding sibling worktrees. The older batch
  run/resume path remains compatibility-only.
- A failed mutating development role may leave clean commits ahead of the trusted isolated origin. Explicit resume archives only an exact latest `provider_failed` linear history under a diagnostic ref and owner-only receipt, restores the unchanged trusted tip, and reruns that failed role; its terminal charge and failed manifest remain immutable.
- Development product roles use the shared trusted ticket-state transition path before provider GO: Planner/Spec-linter run in Planning, Test-author/Builder in Building, and Reviewer/Narrator in Review. Authenticated legacy checkpoints may advance from Ready only through those legal forward states; mismatch refuses only that ticket.
- The disposable lane also has a four-ticket mock-concurrency mode. It reuses the transactional coordinator and CLI runtime with one activated mock account, proves four-way provider overlap and reservation drain, then completes four synthetic role lifecycles; every runtime input must resolve beneath the validated owner-only lane root.
- A development generation may register one to ten tickets while the existing
  dispatch leases and provider coordinator cap active work at four. Its
  accounting manifest binds the exact bundle and base, carries the full
  historical reservation map, and reduces both selected-ticket and aggregate
  budgets. Ticket registration is not durable lane ownership.
- Seed accounting CAS is consumed only after lane construction and planning succeed, while approval output is still withheld. A construction failure retains an unconsumed diagnostic lane; a CAS loser is cleaned without exposing a runnable root.
- A drained ticket may export a one-use v2 passport while unrelated tickets
  remain active. It carries every exact successful role, including Reviewer or
  Narrator, plus the exact next stage; failed attempts remain charged and are
  never promoted. V5 seed accounting binds the passport and full historical
  spend, allowing the ticket to resume under a successor kit without replay.
- A portable operator-await checkpoint may cross an advanced protected product
  base only through one exact live conflicting PR whose product patch matches
  the sealed checkpoint. Replay preserves current protected content only for
  Builder-owned conflicts, records their exact blobs, and then runs Builder,
  fresh Reviewer, and Narrator; test, configuration, control, lock, symlink,
  submodule, rename, or unowned conflicts fail closed.
- Imported checkpoint Spec-linter verdicts are an immutable prefix, not the complete future ticket history. Current-lane verdicts may extend that prefix only when the ordinary current-lane ledger count authenticates each addition.
- Development product export projects only the latest successful Reviewer's non-`factory/` tree changes and rejects later product drift. Exact bundles retain detailed role/retry/audit history but are never applied to canonical product branches; the canonical mailbox deterministically emits one pure final-test commit before one pure implementation commit with the same reviewed application tree. Unsafe or empty strata fail closed, preventing lane controls, route pins, sibling tickets, and unreviewed Narrator metadata from escaping.
- Development product export keeps `root/export` as its default and accepts a distinct new owner-only lane-local output for a later sibling. Every output is an atomic claim outside sensitive lane subtrees; existing, symlinked, outside, or failed targets cannot accumulate or overwrite artifacts.
- Contract 1.7 adds owner-activated subscription-CLI concurrency through the existing transactional coordinator. Activation v2 binds the exact allowed CLI route tuple and canonical provider-policy digest and permits capacity four for Cursor, Codex, and native Claude. Every concurrent attempt receives an owner-only home, config, temporary directory, and credential copy removed only after process-group drain; production roots are lane- and ticket-neutral. Contract 1.6 accepts only API activation v1, and missing or invalid activation remains serialized. Contract 1.8 multi-ticket execution instead refuses missing activation. Budget and permanent denials remain immediate. Role instructions use worktree-relative database-environment paths, and a shared fail-closed sentinel rejects newly added absolute `nysa-sf-dev.*` paths before trusted-host push and at both passport boundaries.
- A product ticket consumes only its own approval and never waits for unrelated global provider idleness. Reviewer reconciliation persists the normalized signed review as quoted ticket evidence; every named repair and rereview is bound to that latest detail instead of reconstructing or substituting another concern.
- The PR-less development Narrator accepts backend-only N/A evidence and
  retains visual tickets with exact `Deferred — publication visual gate`
  markers in Preview and Screenshots. Deferred criteria are not passes; the
  trusted publication step must resolve them before merge. The bundle remains
  development-only and not a production attestation.
- The macOS development-lane controller normalizes its complete process tree
  to the native `C` locale before invoking Git, Perl-backed helpers, or
  providers; caller `C.UTF-8` settings cannot flood evidence with unsupported
  locale warnings.
- The Contract 1.7 development scheduler authenticates durable contract-blocked role manifests against the protected qualification SHA and returns only that qualification ticket to Backlog. Outside qualification it retains Blocked-Escalated. Its lease drains and siblings continue.
- Isolated broker-stage cancellation and deterministic failure release capacity only after token revocation and upstream-request drain are both proven; otherwise the conservative full reservation remains active. Executor success is not durable until bound-container removal succeeds.
- Contract 1.7 concurrent adapters keep timeout and provider descendants in one Factory-owned process group with a kernel-derived start identity. Completed development tickets emit owner-only readiness records and may export independently after only their own attempts, lease, claim, head, and evidence drain. Spec-linter reserves FAIL for material ambiguity or trust/data/external-effect risk and records non-blocking exhaustive coverage as `SPEC-WARN`.
- Cursor Reviewer reconciliation normalizes only exact, matching background-check callback restatements, including the bold `REQUEST CHANGES — FIX-OWNER` form. Missing summaries, conflicting owners or verdicts, and multiple verdict-bearing assistants still fail closed.
- An idle `missing-terminal` controller claim is recoverable only when its
  exact current-kit terminal appears after role exit. Recovery reacquires the
  lease and uses ordinary terminal reduction once; it never relaunches or
  recharges the provider while evidence is absent or mismatched.
- Passportless Planner-preflight recovery is available in both production and
  qualification. Qualification retains its selected-ticket and sealed-artifact
  checks; every lane still requires the exact idle claim, clean remote-equal
  cell, current route, released lease, and absence of run evidence.
- Linear quota cooldowns are shared by the hash of the resolved credential,
  not by product map. Reconcilers using one account make zero API calls until
  the common cooldown expires; legacy releases must remain unloaded or use a
  separate credential until upgraded.
- A selected qualification ticket must pass strict authoring readiness before
  lane construction. Frozen product decisions, one canonical dependency field,
  fixture/authentication seams, and protected-test declarations fail closed
  before claim, lease, worktree, or provider activity.
- A clean non-Test-author role that changes protected ticket evidence is
  charged but never published. The wrapper retains its rejected head under a
  failed-role ref and restores the exact input. Only a successor may retry the
  same role after the passport proves one charge and no completion; a legacy
  un-restored occurrence also requires protected in-flight rewrite authority.

## Log

## 2026-07-31 — Decision 173: Qualification preflight binds the sealed product tree

Category: Incident

The production preflight requires a clean checkout on current `main`. A sealed
production-successor qualification intentionally adds one local control commit,
so applying that branch rule there blocked an authenticated Planner repair
before GO. The qualification launcher now passes the exact product tree already
authenticated by its owner-only activation record. Preflight accepts only a
clean checkout with that tree; ordinary and unbound invocations still require
current `main`.

## 2026-07-31 — Decision 172: Handoffs exclude Git-ignored dependencies

Category: Incident

After authoritative accounting admitted the qualification fallback, the
handoff scanner rejected pnpm's ordinary ignored
`node_modules/@nysa/web` workspace symlink. Handoff snapshots now cover the Git
tree, index, and non-ignored untracked paths, while their filesystem hazard
walk prunes only exact paths reported by Git's active excludes. Tracked and
non-ignored symlinks, hardlinks, special files, nested repositories, and unsafe
parents remain fail closed.

## 2026-07-31 — Decision 171: Fallback reduces authoritative accounting

Category: Incident

The sealed takeover's first successor Builder attempt ended in a terminal
Cursor HTTP 503. Automatic fallback then read the qualification control
worktree's stale ignored runtime ledger even though the ticket worktree had
already written the authoritative terminal manifest through its canonical Git
accounting root. Fallback validation now reuses the ledger reducer over
committed durable rows plus terminal manifests and hashes that exact effective
view. Runtime ledgers remain output-only caches and cannot hide, invent, or
reorder the latest failed attempt.

## 2026-07-31 — Decision 170: Successor qualification is multi-candidate

Category: Incident

The first replacement takeover failed because canonical passports correctly
named the prior qualification candidate while production remained on the
manifest source. Preparation and reduction now validate the ordered release
history plus one contiguous v2 cross-release suffix from the installed source
through intermediate qualification candidates; they neither rewrite history
nor require a false direct source-to-final edge.

## 2026-07-31 — Decision 169: Passport evidence is sequencer history

Category: Incident

The first ordered production-successor qualification preserved all seven
successful T-094 role records in its authenticated passport but the current
runtime ledger contained only the newest Test-author repair. The sequencer
therefore selected ordinary Planner beneath Building and preflight refused.
Contract 1.8 now gives the sole `next-stage` invocation the authenticated
passport's ordered completed-role sequence through a private ephemeral file;
new tickets without passports continue to sequence from the runtime ledger.

- 2026-07-29: Recorded a deferred Cursor Bugbot feedback integration plan
  under `docs/plans/`. It proposes a bounded exact-head read-only audit that
  feeds untrusted findings to the independent Factory Reviewer and existing
  repair-owner flow; no Bugbot behavior, authority, or product setting is
  currently active.
- 2026-07-28: T-089 exposed that a successful Narrator run could commit the required one-line broken-preview report, after which the sequencer incorrectly treated any Narrator ledger row as approval-ready. Sequencing now validates the current unattested bundle structure, permits one repair run after preview recovery, and escalates if that retry is still invalid.
- 2026-07-24: Retained sandbox lanes exposed fabricated cancellation start identities, provider descendants outside the recorded group, repeated equivalent Spec-linter findings, and whole-batch export waits. The development branch now fixes those shared boundaries, preserves prior timing batches, and leaves production activation and Contract 1.6 serialization unchanged.
- 2026-07-24: T-057 reached a new UTC day while fully drained, and checkpoint export incorrectly inherited the ordinary resume day gate. Checkpoint export may now cross that boundary without spending; its v5 successor still carries exact historical charges and consumes a fresh current-day authorization.

- 2026-07-24: Development subscription authorization is bounded to five
  minutes. Native Claude OAuth with less than five minutes remaining is
  `UNAVAILABLE`, allowing the existing profile resolver to select the
  authenticated Anthropic-family Cursor fallback before task submission.

- 2026-07-24: Contract 1.7 development activation now permits four native-Claude calls only with unique attempt-local home, config, temporary, and credential roots. The existing canary selects Codex or Claude and proves four-call overlap; Cursor remains capped at two and Contract 1.6 remains serialized.
- 2026-07-26: Generation 7 exposed that reconciliation discarded Reviewer findings after retaining only verdict and repair owner, so Test-author repaired an unrelated timeout and round 2 approved the unresolved request. Reconciliation now stores the signed normalized detail as inert quoted evidence, binds repair and rereview instructions to it, and a new rolled lane waits two minutes for a busy subscription call without consuming approval.
- 2026-07-24: The development scheduler left tickets Ready through Spec-linter and Test-author even though the role contracts and Hermes require Planning then Building. Role preparation now reuses trusted ticket-state transitions before provider GO, retains shared Reviewer rejection reconciliation, and removes the product-specific Builder-to-Review rewrite; Contract 1.6 and production paths are unchanged.
- 2026-07-24: Reviewer repairs can interleave Test-author and Builder commits, so replaying role history as the product mailbox violated tests-first CI despite an approved final tree. Development export now keeps detailed role history in its exact bundle while deterministically projecting the reviewed application tree into one pure final-test commit followed by one pure implementation commit; unsafe or empty strata and tree drift fail closed.
- 2026-07-24: Cursor aggregated one valid Reviewer assistant response into its terminal result twice, causing strict Contract 1.7 repair ownership to appear duplicated. Reviewer parsing now prefers exactly one terminal-bound verdict assistant while rejecting ambiguity or contradiction, and retained development lanes reconcile through the validated invoking controller instead of their stale pinned helper.
- 2026-07-24: Cursor can leave its hardcoded scratch bridge as a safe empty directory tree after replacing the lane symlink, which made the next development plan fail before GO. Preclaim now reuses the existing fail-closed empty-tree validator only after subscription-provider idle proof, then atomically claims the bridge as the lane symlink; every unsafe or active shape still refuses.
- 2026-07-25: Contract 1.7 product Cursor attempts no longer consult or claim the legacy serialized scratch bridge. Attempt-local path and ownership checks remain fail closed, Cursor stays capped at two pending a real-provider canary, and legacy Cursor lanes retain bridge serialization.
- 2026-07-24: Seed checkpoint import scanned from the pristine product base and rejected an authenticated prior lane-control `PROJECT.env` path as provider output. Import now proves linear ancestry and the exact first-commit lane-control identity/path scope before scanning only later commits; provider-added stale paths still fail, and the old control commit is never replayed over the new lane configuration.
- 2026-07-24: Resume planning attempted to hash an optional absent per-ticket envelope, emitting a misleading `shasum` warning. Each original ticket now binds to its owner-only regular ticket envelope when present or the effective global envelope otherwise; unsafe overrides fail closed and either envelope's drift invalidates the resume basis.
- 2026-07-24: A fixed `root/export` target prevented a later resumed sibling from exporting after an earlier sibling succeeded. Product export now retains that default while allowing one explicitly named new lane-local output, with sensitive-path refusal and cleanup of failed claims instead of append or merge semantics.
- 2026-07-24: A corrected controller could not export an older pinned retained lane because it looked for the newly introduced lane-path sentinel inside that old kit, and targeted exports then rejected or forgot the full original-ticket charge universe. Checkpoint boundaries now keep exact lane pin validation while running the trusted invoking controller's sentinel; selected records may retain a superset charge map, and repeated chaining derives that universe from the retained checkpoint while remaining exactly bound to v5 accounting.
- 2026-07-24: Checkpoint export initially scanned from the pristine product base and therefore mistook the Factory-owned lane-control commit for provider output. Export path scanning now begins at the recorded `lane_control_sha`, leaving seed validation at the pristine base and scanning every role commit.
- 2026-07-24: A current-lane Spec-linter verdict after a Planner-only checkpoint was incorrectly rejected because sequencing compared the evolving ticket verdict list to the complete checkpoint list. Checkpoint verdicts now remain an exact immutable prefix, while the existing ledger-to-verdict reconciliation authenticates later current-lane additions.
- 2026-07-24: A drained four-ticket lane exposed that targeted resume still resolved every original sibling and required excluded tickets to be complete, so one blocked contract prevented an unrelated Reviewer retry. Resume now validates runnable stages only for the selected subset while binding every original sibling's clean head, origin, tree, ticket, route, envelope, and evidence; subsequent attempts retain the original selection universe and excluded drift fails closed.
- 2026-07-24: A retained Planner contract copied its first lane's physical database-environment path and a third Cursor call timed out twelve seconds before capacity released. Development roles now receive a portable worktree-relative database path, role push and checkpoint import/export reject added absolute lane paths, and coordinator-owned transient-capacity waiting uses its existing fifteen-minute bound without changing budget, cancellation, or production routing.
- 2026-07-24: The first real cross-kit checkpoint retry exposed that its importer expected a route-only commit even though the trusted model manager atomically commits the route plan and ticket Kit-SHA. Import now validates that exact two-file shape, proves Kit-SHA is the only ticket change, discards the stale pin, and writes the current development-kit binding.
- 2026-07-24: That same retry showed seed authorization was consumed before lane construction, so a fail-closed import error could burn a zero-provider reservation. Planning output is now withheld until construction succeeds and the lineage CAS is consumed; failed construction remains retryable, while a CAS loser is cleaned before any runnable root is exposed.
- 2026-07-24: Checkpoint planning then exposed that the non-provider helper environment omitted the validated lane root, so exact checkpoint stage reproduction refused while direct subscription helpers passed. All trusted development product helpers now carry the same lane-root and internal-sandbox binding used by provider execution.
- 2026-07-24: A second corrected-kit retry exposed that checkpoint export saw only current-lane manifests and discarded imported successful roles. Import now retains the exact source checkpoint, and export validates its hash/import/head bindings before chaining the prior records ahead of current successes.
- 2026-07-25: Qualification runs showed that authenticated missing-contract results still went to Blocked-Escalated even though Ready-ticket omissions belong back in Backlog. The trusted transition now binds the protected qualification ticket and exact kit SHA, accepts completed or exactly matched conservative accounting, returns only that ticket to Backlog, and leaves siblings running.
- 2026-07-25: A real qualification then showed that Spec-linter FAIL still entered the ordinary replan loop and eventually requested round-three authorization. The development scheduler now returns the ticket directly to Backlog after the first authenticated qualification FAIL and leaves siblings running.
- 2026-07-23: A real four-ticket pilot exposed that a Builder could correctly commit an impossible frozen-contract blocker while the development scheduler treated it as a generic resumable failure. Contract 1.7 now authenticates that durable wrapper result, transitions only the affected non-qualification ticket to Blocked-Escalated, drains its lease, and continues siblings without replay.
- 2026-07-23: A corrected development kit could not safely resume retained ticket work because same-lane resume pins the old kit while an ordinary seed intentionally forgets lane-bound role evidence. Development-only pre-Reviewer checkpoints now bind exact successful evidence and cumulative accounting across a new-kit seed; failed roles are not promoted, and current-kit Reviewer/Narrator evidence remains mandatory for export.
- 2026-07-24: T-046 exposed contradictory development Narrator guidance: backend HTTP contracts were denied Preview N/A while the trusted validator required two N/A markers. The development marker now accepts no-browser/no-visual HTTP APIs with backend-only Screenshots N/A and either Preview N/A or an explicit pending publication preview, without weakening the later production gate.
- 2026-07-23: Development product concurrency stopped duplicating provider policy in its scheduler. Eligible roles now enter the existing coordinator, which owns bounded pre-GO waiting and atomic budget admission; Codex and the mock account can prove four-way overlap while Cursor and native Claude retain their lower safety caps.
- 2026-07-23: Retained T-039–T-042 evidence showed that automatic readiness retries and independently seeded sibling lanes consumed conservative reservations without a fresh control decision. Development role failures are now terminal with an explicit same-lane resume handoff, and seeded plans atomically advance a shared accounting lineage before creating a runnable lane.
- 2026-07-23: The development scheduler and Hermes previously interpreted successful Reviewer output through different write paths. Contract 1.7 now uses one trusted ticket-state reconciliation that binds the read-only head and output digest, records verdict plus repair ownership, and commits Review-to-Building atomically; Contract 1.6 remains unchanged.
- 2026-07-23: Retained resume readiness exposed that the sanitized CLI environment no longer changed into the lane root, causing Codex to load lane configuration from an unrelated caller worktree and fail with a permission error. The shared subscription boundary now fixes both HOME and working directory to the validated lane root.
- 2026-07-23: A retained product resume showed that three immediate Codex authentication probes could all land inside one short lane-local session transition. Readiness now keeps the same three-attempt ceiling but waits one second between misses; exhaustion still stops before approval consumption, leases, reservations, or task submission.
- 2026-07-23: The same resume exposed credential-byte drift when readiness occurred only after plan hashing and repeated basis probes. Resume planning and execution now establish readiness first, bind the resulting bytes, and reuse the execution proof inside the scheduler; pinned routes retain their independent pre-reservation check.
- 2026-07-23: Cursor reauthentication in a retained lane exposed an implicit macOS credential-store dependency. The sanitized subscription and provider-task boundaries now explicitly select Cursor's file-backed store inside the lane-local session home, keeping login, refresh, readiness, and calls isolated from host credential state.
- 2026-07-23: A seeded real-ticket retry exposed that under-lock envelope resolution reloaded the product default and discarded the lane's reduced per-ticket envelope. Effective resolution now takes the already-validated ticket envelope as its base before applying immutable overrides, so the transactional coordinator receives the seed-adjusted cap.
- 2026-07-23: Concurrent Cursor Reviewer startup exposed a transient pinned-route authentication miss after both Builders had committed. The existing one-time pre-submission retry now recognizes exact authentication/model availability reasons while refusing identity, version, contract, and post-submission failures.
- 2026-07-23: Real-ticket audit found that whole-branch development patches could include lane controls and sibling ticket records. Export now reuses the trusted latest-Reviewer evidence, rejects post-review product drift and unsafe modes, excludes the complete Factory namespace, and marks exact bundles as retry/audit material only.
- 2026-07-23: A retained concurrent product resume exposed a false-positive outer subscription check: ambient state could satisfy readiness while the sanitized role environment correctly refused Codex authentication. All subscription/product probes, version evidence, approval hashes, and role environments now share one clean lane-local session boundary; no coordinator, budget, lease, fallback, or production serialized behavior changed.
- 2026-07-23: The real four-ticket development proof exposed that retrying every sibling wastes completed work and that fresh disposable lanes could forget prior aggregate reservations. Seeded resumes now select one to four unfinished tickets while retaining the full bundle-bound accounting map, and claim rollback, source sentinels, and seed-mode validation prevent stranded leases or pre-sandbox path escape.
- 2026-07-23: The operator authorized a bounded $200 per-ticket and $700 aggregate ceiling to finish the retained real-product development proof. A v3 accounting record carries that exact authorization while v2 and production limits remain unchanged.
- 2026-07-22: Contract 1.7 defined `cli-concurrent-v1` for the existing Codex, Claude Code, and Cursor adapters without API credentials. Limits remain exclusively policy-owned, with Cursor initially capped at one because its scratch root is account-global; focused compatibility coverage preserves Contract 1.6 serialization.
- 2026-07-22: Production queueing exposed dispatch renewal waiting behind the provider launch lock until reporting `launch lock stuck`. Renewal now bypasses that unrelated long lock, retains exact lease ownership, and checks KILL/MAINTENANCE on both sides of its atomic replacement.
- 2026-07-22: Superseded Nysa tickets exposed that Linear's existing Canceled column was only cosmetic. Canceled is now a validated terminal ticket state, Backlog-to-Canceled is an operator transition, and focused reconciliation coverage proves its projection.
- 2026-07-22: A blocked four-call loopback drill proved targeted broker cancellation, replacement admission, timeout drain, full-reservation accounting, token cleanup, and zero remaining capacity. Runtime convergence now requires revocation plus no in-flight broker request, while executor result publication follows proven container removal.
- 2026-07-22: The isolated development lane admitted four budget-bound synthetic provider attempts concurrently, measured a 2.02-second overlapping interval, drained every reservation, and completed four six-role ticket lifecycles in 259 seconds. The exercise added lane-root containment assertions and did not change production activation, leases, credentials, services, or the legacy serialized path.
- 2026-07-22: The first production T-032 ticket-PR gate exposed that the launcher sent `ticket-pr.py` through `/bin/bash`. The route now uses the fixed Python interpreter with `-I -S`, and the sealed-launcher contract test requires Python-produced ticket-PR JSON before any release.
- 2026-07-22: Retrying T-032 with the corrected launcher exposed that `ticket-pr.py` did not pass its canonical lease to `next-stage`, while the prior launch failure had already left the reviewed ticket without its early PR. The helper now propagates and revalidates the lease, and its bounded Narrator recovery validates reviewed-head lineage before any GitHub access.
- 2026-07-22: T-032's successful Cursor Reviewer was initially invisible to `ticket-pr.py` because conservative billing uses `accounting_state=abandoned_conservative`. Reviewer lookup now requires completed, role-valid execution while accepting that exact conservative accounting state; nonterminal or role-invalid manifests remain ineligible.
- 2026-07-22: In-flight activation found that a valid historical Cursor identity and CLI version could no longer pass the candidate catalog or runtime drift checks. V2 release migration now preserves its parent-hashed history and exact logical routes while refreshing only probe-reported adapter versions and identities; every other route tuple field remains immutable and any unavailable or changed route fails closed.
- 2026-07-22: Nysa certification exposed that a product's managed secret scanner could not bootstrap inside the network-denied certification sandbox. Certification now reuses the existing pinned-scanner staging helper for the disposable product copy before running product checks.
- 2026-07-21: A second release cutover exposed that protected in-flight authorization accepted only v1 plans even though a previously migrated ticket already carried a valid v2 journal. Exact source-bound v2 journals may now cross releases through the existing preview-hash flow; migration appends one parent-hashed release-affinity revision and preserves all prior route and fallback history.
- 2026-07-21: The real disposable Reviewer returned the canonical role-contract verdict `## Verdict: Approve`, but the lane required uppercase `APPROVE` and stopped before Narrator. The lane now accepts only a standalone `Approve` or `Verdict: Approve` line case-insensitively; surrounding prose such as `do not approve` remains rejected.
- 2026-07-21: The real disposable lifecycle completed Planner through Builder before a transient empty Cursor model-list result rejected the pinned Reviewer route; the identical lane environment immediately reported the exact route READY afterward. Cursor readiness now retries that read-only listing once without delay, while two misses and every version, family, or reported-identity mismatch still fail closed before submission.
- 2026-07-21: Cursor CLI `2026.07.20-8cc9c0b` resolved `claude-fable-5-thinking-medium` to the stable runtime identity `Fable 5 300K Medium` during the real disposable lifecycle, despite the model-list label retaining the older 1M wording. The fail-closed catalog now matches the provider stream identity used for evidence validation.
- 2026-07-21: The real development-lane probe confirmed that the authenticated Cursor CLI session fails closed when nested under macOS Seatbelt even though its credential helper succeeds there. Real lane calls now use Cursor's explicit internal sandbox while mock calls retain outer default-deny Seatbelt; this exception is lane-only and does not change production routing or release certification.
- 2026-07-21: A reusable disposable development lane now exercises Planner through Narrator without installation, registration, certification, Linear, GitHub, or Nysa state. Its focused suite verifies the exact role order, local push, network-denied mock sandbox, owner/inode-bound cleanup, production sentinels, one-use Cursor approval, and the 15-minute mock ceiling. The launcher also avoids Bash 3.2's empty optional-array expansion while preserving the existing reviewer-only fallback exception.
- 2026-07-21: A Nysa release activation exposed that protected-merge reconciliation incorrectly required its historical target pin and companion paths to remain unchanged forever. Validation now anchors those values at the original atomic adoption commit while preserving no-touch history for the reconciliation directory and terminal projections, allowing later authorized release and T-032 evolution without weakening the historical batch.
- 2026-07-21: The T-032 pre-release audit found that protected in-flight authorization excluded Ready tickets even though route pinning occurs before the first role. Ready is now accepted under the same exact protected-main authorization, branch-head, old-plan, maintenance, and zero-lease barriers, and migration preserves its state.
- 2026-07-21: The first T-032 Cursor-first planner canary switched itself into Cursor Plan mode and returned without the required commit. The shared Cursor adapter now ends every factory prompt with an explicit default-execution-mode requirement; a disposable live pre-release canary on pinned Cursor CLI `2026.07.17-3e2a980` created exactly one requested file, committed it, and exited cleanly without a mode switch. Wrapper no-commit enforcement remains the fail-closed backstop.
- 2026-07-21: Release migration now verifies the exact protected factory SHA's six shards plus aggregate/immutability, installs the inert candidate, prepares one clean canonical product tree with pin/budget/migration evidence, updates CLIs/plugins, certifies that exact tree, and only then opens the product PR and activates. The Nysa cutover reconciles T-024/T-030/T-031 as one protected batch and proves the release with T-032 alone before parallel dispatch.
- 2026-07-21: Software Factory pull requests run targeted or policy-only deferred checks; only protected `main` runs the complete suite, split into three balanced shards per platform. The aggregate remains the release gate, and install/certification require all six successful protected-main shard jobs plus aggregate/immutability evidence and local smoke. Host migration verifies pinned CLIs and both Nysa Agents plugin installations before certification.
- 2026-07-21: Cursor-first `cursor-balanced-v2` becomes the no-record model default without lowering role effort. Release migration explicitly activates its exact hash for Nysa, validates the operator-approved $100 ticket budget, and transfers the ignored production Linear map only after the old reconciler stops. Linear reconciliation caches immutable protected-main migration batches, treats equivalent Markdown as unchanged, tracks evidence by digest, verifies mutation success, paginates recent fallback approvals, and assigns both operator queues.
- 2026-07-21: Contract 1.6 now defaults the single coupled ticket-worktree/provider capacity to four. Contracts 1.1–1.5 retain their default of one.
- 2026-07-21: Normal Done validation anchors the receipt's complete-ledger digest to the unique immutable closeout commit, then requires protected main's current ledger to preserve those bytes as an unchanged prefix. Later ticket rows may append without invalidating earlier terminal evidence; historical ledger rewrites still fail closed.

## 2026-07-25 — Decision 57: Sandbox qualification is one immutable rolling generation

Category: System change

A protected Contract 1.7 qualification manifest binds one exact Factory SHA to
ten ticket IDs, tracked dependencies, capacity three ramping to four after
three protected Done results, and a hard stop at ten; a started ticket beyond
ninety minutes invalidates the generation. Protected `Merge-Policy: auto`
preauthorizes the existing Linear approval path, while a first terminal Cursor
availability failure may preserve its handoff and use the role-appropriate
direct CLI for the second and final attempt. Spec-lint failure returns the
ticket to Backlog for operator repair without stopping eligible siblings.

## 2026-07-26 — Decision 58: Development product lanes are Cursor-first

Category: Decision

Isolated product lanes activate `cursor-balanced-v2`; authenticated Cursor
routes own production and checking roles, while direct Codex and Claude are
fallbacks. The first qualification generation exposed and invalidated the
development controller's contradictory native-first override before any
product merge.

## 2026-07-26 — Decision 59: Real development roots stay short

Category: System change

Real development lanes use owner-only randomized roots under `/private/tmp`;
the trusted test harness retains its isolated caller-provided parent. The
second qualification generation exposed that macOS's long per-user `TMPDIR`
made otherwise-correct attempt-local Cursor data paths exceed the CLI limit
before submission.

## 2026-07-26 — Decision 60: PR-less visual evidence defers, never passes

Category: System change

The isolated development lane has no PR preview or browser network, so its
Narrator may retain a visual ticket only by marking both Preview and
Screenshots `Deferred — publication visual gate` and naming the exact checks.
The deferral is not acceptance; trusted publication must resolve it before
merge, while normal production Narrators still require live evidence.

## 2026-07-26 — Decision 61: Development controllers use the macOS C locale

Category: System change

The development-lane entry point exports `LANG`, `LC_ALL`, and `LC_CTYPE` as
`C` before any helper runs. This prevents a caller's unsupported Linux
`C.UTF-8` locale from flooding macOS Git/Perl evidence while retaining
deterministic byte-oriented controller behavior.

## 2026-07-26 — Decision 62: Rolling-ten recovery uses a final-four stability gate

Category: Decision

Sofia approved the recovery plan in
`docs/evidence/2026-07-26-sandbox-factory-rolling-ten-recovery-handoff.md`:
Generation 9 may use up to four concurrent Cursor sessions, broad CI and
Hermes verification run only in GitHub, pixel-perfect comparison is advisory,
and operator finish edits are documentation-only. T-077 is already merged and
must be reconciled rather than rerun; stability requires the final four
accepted tickets to complete under one unchanged executable Factory SHA with
no operator non-documentation edits or successful-role replay. This records an
operating decision only and makes no Factory runtime or production change.

## 2026-07-21 — Decision 54: Cursor-first routing preserves balanced-v2 quality

Category: Decision

`cursor-balanced-v2` reverses only the primary/secondary transport order from
`balanced-v2`; every role keeps the same model tier and effort. It is the
no-record default, while explicit active profiles and committed ticket plans
remain unchanged. Nysa activates it only through the later release migration,
with exact CLI/plugin pins and an operator-approved $100 per-ticket envelope.

## 2026-07-21 — Decision 55: Linear synchronization is snapshot-idempotent

Category: System change

Protected terminal migration batches are validated once per physical repository
and immutable protected commit while per-ticket conflict checks remain exact.
Linear projection canonicalizes equivalent Markdown, advances local markers only
after confirmed mutations, identifies posted evidence by content digest, reads a
complete fallback-approval window, and assigns both Awaiting Approval and
Blocked-Escalated to the configured operator. Git remains execution authority;
webhooks, new workflow states, and speculative API batching remain deferred.

## 2026-07-21 — Decision 56: Development lanes are disposable, not releases

Category: System change

A clean committed factory branch may run the six-role synthetic lifecycle in an
owner-only macOS Seatbelt lane with a separate product, local origin, runtime,
home, and worktrees. Mock work is network-denied; real Cursor work requires a
dedicated key and a one-use approval bound to the exact executable and lane
inputs. Development artifacts cannot be activated, and protected-main CI,
sealed installation, live canary, product certification, and activation remain
mandatory for production.

## 2026-07-21 — Decision 53: Full factory verification is GitHub-owned

Category: System change

Local readiness and pull requests run focused or policy-only checks and record
broad coverage as deferred. Every protected-main commit runs the complete
Linux and macOS registry across three shards per platform. Installation and
certification require exact successful main evidence and local platform smoke;
they never fall back to the complete local suite. Migration verifies the
execution host's pinned CLIs and Codex/Claude Nysa Agents plugins before
certification.
- 2026-07-20: Contract 1.6 adds one-shot autonomous dispatch, exact worktree and lease claiming, early idempotent ticket PR creation at the Reviewer boundary, and activation-gated isolated API-route execution. The supervisor remains an inert Hermes profile skill until an operator enables the existing scheduler; this host has no real-provider activation because its dedicated broker credentials and TLS configuration are absent.
- 2026-07-20: Contract 1.6 expands the coupled worktree/provider capacity to six while retaining a default of one. Contracts 1.1–1.5 remain bounded at four and Contract 1.0 remains unchanged; Decision 51 records the completed isolated-runtime gate while preserving serialization for legacy routes.
- 2026-07-20: Concurrent Done closeouts may project an identical ledger after an earlier closeout already captured every terminal run. The trusted Done validator now permits `factory/ledger.csv` to be absent from that later closeout commit while still binding and checking the complete ledger hash; ticket and Done-attestation paths remain mandatory and exclusive.
- 2026-07-20: A patched control-plane release may finish Done for already-approved evidence from an older ticket-pinned release. The closeout validates and records the ticket's canonical `Kit-SHA`; bundle and approval remain bound to the active release, so prior role evidence is never relabeled.

## 2026-07-21 — Decision 52: Contract 1.6 defaults coupled capacity to four

Category: System change

When `MAX_CONCURRENT_TICKETS` is absent, Contract 1.6 admits four concurrent
ticket worktrees and provider calls. It remains the only product capacity
setting; explicit values `1` through `6` remain valid, and Contracts 1.1 through
1.5 keep their default of one.

## 2026-07-20 — Decision 51: Isolated routes and autonomous dispatch remain explicitly gated

Category: System change

Contract 1.6 admits parallel provider work only for an exact owner-activated API
route through the transactional coordinator, short-lived credential broker,
networkless digest-pinned worker, and trusted artifact controller. Every native
subscription or Cursor CLI route stays behind the legacy product-wide lock, and
missing or malformed activation fails closed. Hermes autonomous operation is a
one-claim supervisor invocation over the stable launcher: it deterministically
waits at capacity, creates or reuses one exact open PR only when Reviewer is the
next stage, delegates one ephemeral dispatcher child, and never approves,
merges, loops, scans mutable tickets, or installs its own scheduler. Autonomous
claiming requires configured capacity above one because a capacity-one run has
no lease capability to transfer to the child.

## 2026-07-20 — Decision 50: Contract 1.6 couples capacity at six (default superseded by Decision 52)

Category: System change

`MAX_CONCURRENT_TICKETS` remains the only product capacity setting and defaults
to `1`. Active Contract 1.6 accepts `1` through `6`; Contracts 1.1 through 1.5
remain bounded at `4`, and Contract 1.0 semantics are unchanged. The setting
couples exact-worktree ticket leases with eventual provider-call capacity, but
the product-wide provider lock remains for legacy routes; Decision 51 defines
the exact activation gate for isolated API-route parallelism.

## 2026-07-19 — Decision 49: Validated pre-submission checks get a bounded two-minute gate

Category: System change

Protected-history validation can legitimately exceed the original ten-second
process-group acknowledgement window. The isolated wrapper now waits at most
two minutes for the trusted controller's final GO gate; adapters still cannot
start before acknowledgement, a missing controller remains a hard timeout, and
kill, maintenance, and targeted cancellation are rechecked immediately before
the gate opens and again by the isolated wrapper after it observes the gate.
The wrapper's second check is the submission boundary; later controls use
normal post-submission drain semantics. A boundary stop exempts only its exact
control record from checkout comparison; all manifest, claim, lock, ledger, and
unrelated checkout integrity checks still run.

## 2026-07-19 — Decision 48: Targeted PR CI and remote full-suite reuse (superseded by Decision 53)

Category: Decision

Linux and applicable macOS pull-request jobs execute the same fail-closed
component selector in parallel; protected `main` remains complete on both
platforms. Local readiness may explicitly defer only broad behavioral work to
required GitHub CI after policy checks. Installation corroborates the exact
main SHA against authenticated GitHub Actions push-run jobs and runs a local
sandboxed platform smoke instead of repeating the hour-long suite. Missing or
invalid remote evidence falls back to the full local suite, and
`CI_FORCE_FULL=1` disables both deferral and reuse.

## 2026-07-19 — Decision 47: Shadow fail-closed dynamic CI selection

Category: Decision

Committed diffs classify by dependency surface rather than line count. Six
audited leaf components are active for local changed-file selection; additions, deletions, renames, unknown
or shared paths, multiple components, and invalid comparisons run full. Each
component needs three real shadowed diffs, zero reproducible misses, and a median
targeted duration at most half of full with at least ten local minutes saved.
This shadow requirement was superseded by Decision 48 after the six mappings
met their activation evidence. Any reproducible miss still demotes that
component to shadow and resets its evidence.

## 2026-07-19 — Decision 45: Local multi-project operator control plane

Category: Decision

Contract 1.5 adds a loopback-only console that selects only registered projects
and reaches product state through fixed launcher grammars. Project model policy,
per-role envelope limits, bounded overrides, and targeted attempt cancellation
are preview-hashed mutations; active budget changes use cancel, conservative
accounting, and same-role restart rather than retroactive manifest edits.
Normal Reviewer family separation remains mandatory, with only an exact
ticket-scoped one-use Linear fallback exception.

## 2026-07-19 — Decision 46: Dispatcher lease capacity expands to four

Category: System change

`MAX_CONCURRENT_TICKETS` accepts only `1` through `4`, defaults to `1`, and
requires one opaque lease per ticket above `1`. Atomic allocation refuses a
fifth lease, stale records continue consuming capacity until owner renewal or
maintenance recovery, and the product-wide provider lock continues to
serialize model-provider intervals.

## 2026-07-18 — Decision 44: Balanced v2 raises default effort and adds Cursor Fable

Category: Decision

The no-record profile becomes `balanced-v2`: production remains Codex Sol for
Planner and Terra for Builder/Narrator at high effort; checking remains Claude
Fable for Spec-linter/Test-author at medium and Sonnet for Reviewer at high.
Cursor secondaries use Sol High, Fable Thinking Medium, and Sonnet Thinking
High respectively. `legacy-balanced-v1` remains immutable compatibility policy,
and historical catalog hashes are accepted during migration only when every
selected route tuple still matches current certified policy.

## 2026-07-18 — Decision 43: Pre-contract terminal evidence is a separate bounded batch

Category: System change

The T-001 through T-012 pre-contract terminal epoch uses an independent
terminal-backfill schema and directory. Authorization binds the exact ticket
batch, product repository, protected-main basis, target kit, cutoff, immutable
source blobs, implementation and closeout PR ancestry, ledger rows, and
authentic app-owned checks. Historically absent bundles and Kit-SHAs remain
null. Only one manual protected product merge makes the complete batch
authoritative, and overlap with normal or first-batch evidence fails closed.

## 2026-07-18 — Decision 42: Earliest legacy PRs bind their authentic check era

Category: System change

T-013 through T-016 predate separate `policy` and `app-tests` jobs, so their
one-time allowlisted migration class binds the authentic app-owned aggregate
`ci` and `test-immutability` checks plus independent criteria-audit and current
combined-test digests. No other ticket may use that class; later reviewed
tickets still require all four exact app-bound checks.

## 2026-07-18 — Decision 41: Legacy closeout preserves the Contract 1.3 trust boundary

Category: System change

The one-time Contract 1.2 migration uses exact authorization and per-ticket
receipts under `factory/migrations/contract-1.3/`, never synthetic normal
attestations. A deterministic local generator binds immutable Git/GitHub,
check-app, ledger, audit, cutoff, old/new kit, and protected-basis evidence but
performs no remote mutation; only the operator's manual merge of the complete
single product PR grants authority. Review is the normal legacy class, while
only T-019/T-020 may carry the separately audited Planning anomaly class.
Shared validation makes plain Done, partial batches, unknown files, conflicting
receipts, and wrong target kits fail activation and terminal sequencing.

## 2026-07-18 — Decision 40: Model portfolios pin exact ticket routes

Category: System change

The operator activates an ordered model profile by exact preview hash.
`legacy-balanced-v1` was the default at this decision and is superseded as the
no-record default by Decision 42; OpenAI-, Claude-, and
Cursor-priority profiles provide explicit alternatives. Ticket-boundary
pinning resolves all six roles with distinct production/checking families and
commits the Kit-SHA plus exact route plan in one verified push; roles never
re-resolve, exact-route re-probe is allowed, and post-submission retry is not.
Kimi remains disabled and unpiloted, and scoped budgets remain future work.

## 2026-07-17 — Decision 39: Exact sealed releases reuse bounded suite proof

Category: System change

Fresh install or certification records atomic owner-only kit-suite evidence bound to the exact SHA/tree/origin/release path, recomputed physical tree, host, OS/architecture, suite definition, tool version, and configured lifetime. Certification treats missing or invalid evidence as a cache miss under the existing install lock, refreshes it only after a successful isolated suite and release revalidation, always reruns product certification and product binding checks, and issues schema-2 receipts bound to the exact suite proof with expiry capped by it.

## 2026-07-17 — Decision 38: One Linear approval enables protected auto-merge

Category: System change

Contract 1.3 makes the operator's Awaiting Approval → Approved Linear transition the sole business approval. Trusted bundle and approval attestations bind the latest non-void approved review, later Narrator lineage, bundle blob, exact PR/head, role runs, kit SHA, configured merge method, and newer Linear observation before requesting ordinary protected auto-merge; Done starts exactly at authoritative main and binds the protected approval blobs, exact merged PR head, collision-free configured merge-commit contexts, and projected accounting. Done then owns one retryable closeout commit and exact factory metadata/accounting PR, requesting protected auto-merge without another approval. At concurrency two every attestation requires the matching opaque lease without recording it; only protected-main terminal evidence produces `COMPLETE` and authorizes release. Contract 1.2 remains fail-closed at Review.

## 2026-07-17 — Decision 37: Required CI gets a documentation fast path

Category: Decision

The required workflow remains reporting and fail-closed. Diffs limited to the explicit inert-metadata allowlist skip expensive suites while retaining repository, secret, artifact, and immutability checks; every other change runs Linux. Pull requests add macOS only for shell/platform-sensitive paths, while every non-lightweight merged kit SHA runs both platforms before release and instantiated products run full verification on every `main` push. Commit size and Markdown suffixes are not safety signals; ambiguous comparisons fail closed.

## 2026-07-17 — Decision 36: Two-lease pilots expose the next latency boundary

Category: Decision

Two Nysa pairs completed with concurrent leases, disjoint worktrees, serialized provider intervals, and protected sequential merges; the second pair satisfies the evidence prerequisite for a non-production provider-concurrency canary, not production parallel calls. The pilots exposed avoidable latency from unconditional product CI and previews, repeated certification, sequential PR creation/checks, operator polling, and rebasing the second PR. They also exposed contract gaps around noninteractive CLI probes, just-in-time ticket-ref verification, deleted remote-tracking refs, ticket-cut approval validation, batch ledger projection, and trusted bundle/merge/deploy/Done attestation; these are tracked in `TODOS.md`.

## 2026-07-16 — Decision 35: Borrow mechanisms, not another factory

Category: Decision

Research across metaswarm, SWE-AF, Flow-Next, Open SWE, OpenHands, SWE-agent, SWE-ReX, E2B, and adjacent infrastructure found no project that combines protected test authorship, enforced cross-family review, hard spend accounting, exact-SHA releases, durable ticket state, and evidence-based human approval. Preserve that control plane; selectively borrow Flow-Next-style requirement-to-evidence traceability, metaswarm-style independent review evidence, Open SWE trigger/idempotency patterns, and in-toto/SLSA-shaped provenance fields, and test SWE-ReX only as a pinned execution transport behind the launcher. Do not add a second orchestrator, tracker, model-budget authority, or observability stack during the walking-factory phase.

## 2026-07-16 — Decision 34: Legacy ledger lifecycle pairs remain readable

Category: System change

Pre-manifest products may retain a durable reservation row followed by its terminal row under one run ID. The effective reducer collapses only that ordered pair when stable run identity matches exactly; reversed, repeated, or identity-conflicting duplicates still fail closed.

## 2026-07-16 — Decision 33: Spec-linter returns to Fable

Category: Decision

The Haiku Spec-linter cost experiment is superseded before product adoption because existing certified product profiles and the approved Nysa concurrency pilot retain Fable routing. Spec-linter and Test-author both use Claude Fable 5/medium; no other role route changes.

## 2026-07-16 — Decision 32: Provider-lock handoff is atomic

Category: System change

Concurrent launch certification exposed transient false-stale observations and an ownerless teardown window at the serialized provider lock. Launch now debounces bounded owner-liveness misses, atomically renames its owned provider lock before cleanup, and uses the configured run timeout for both launch- and provider-lock waits; stale or unsafe locks still require operator recovery.

## 2026-07-16 — Decision 31: Escalation-latency and reservation improvements

Category: System change

Evidence from the first 12 Nysa tickets showed operator waits — not code review — dominate cycle time (50% Blocked-Escalated; 70% of stall minutes in a few multi-hour gaps). Planner v4 now consults and appends a durable `factory/rulings.md` (immutability-exempt path) before escalating, checks cross-ticket file boundaries and deploy topology pre-freeze, and the Linear reconciler assigns Blocked-Escalated issues to the API key's viewer for native push notification. The ticket reservation shrinks to the remaining ticket budget (adapter hard stop and telemetry fallback follow it), so a nearly finished ticket launches instead of stalling on flat-reserve arithmetic; daily/global cap checks keep the flat reserve. Spec-linter moves to Claude Haiku 4.5/medium as a cost pilot; Reviewer and Planner routing are unchanged. Railway runbook adds PR-environment reference variables and a project token for non-interactive redeploys.

## 2026-07-16 — Decision 30: Provider-lock recovery is explicit and evidence preserving

Category: Decision

Ordinary launch never reclaims a stale, malformed, or ambiguous product-level provider lock. After publishing KILL and draining recorded processes while holding the launch lock, the kill switch may atomically quarantine only an unchanged, structurally safe lock whose PID/start identity proves stale; every unsafe case remains for operator reconciliation.

## 2026-07-16 — Decision 29: Provider intervals serialize until OS isolation

Category: Decision

Contract 1.2 may retain two independent ticket leases, but a product-level control lock serializes provider intervals from before manifest creation through provider exit and integrity verification. During that interval every new or changed sibling manifest fails closed; parallel provider execution remains deferred until a separate UID or enforced sandbox prevents providers from authoring launcher control state.

## 2026-07-16 — Decision 28: Contract 1.2 stops at Review (supersedes Decision 23 and the approval portion of Decision 26)

Category: System change

Contract 1.2 has no trusted bundle-attestation boundary. Ticket-state therefore refuses transition or materialization of Awaiting Approval and Approved, sequencing never authorizes `AWAIT-MERGE`, and Done remains unavailable pending a separate merge/deploy attestation path. Deterministic ledger projection also refuses every active or ambiguous claim and every `factory/runs/*.pid` record so close-out cannot omit potentially live work.

## 2026-07-16 — Decision 27: State ownership and projection are committed

Category: System change

Operator state materialization is restricted to Backlog-to-Ready and the declared non-sensitive Blocked resume. Linear projection reads the exact ticket ref or committed HEAD and skips files with no committed source. Approval materialization described here is superseded by Decision 28.

## 2026-07-16 — Decision 26: Ticket evidence must be clean and remotely current

Category: System change

Contract 1.2 refuses tracked and untracked worktree dirt before ticket helpers. Supported pending operator fields must pass through materialization before factory-stage transitions. The approval-readiness portion of this decision is superseded by Decision 28: approval overlays now refuse because contract 1.2 has no trusted bundle-attestation path.

## 2026-07-16 — Decision 25: Cutover and config read authoritative sources

Category: System change

Release cutover checks nonterminal kit affinity from each committed exact ticket branch rather than stale registered-main ticket content. Plain configuration clears every allowlisted key before loading, including when `global.env` is absent, so only file-provided machine policy survives.

## 2026-07-16 — Decision 24: Fresh branches and Project removal are explicit

Category: System change

Dispatch creates an exact clean ticket worktree from protected main and uses trusted materialization to create and verify its remote ref before preflight. Removing every Linear Project writes an explicit initiative tombstone into the ignored overlay, preventing stale membership from being reattached and making the ticket ineligible until reassigned.

## 2026-07-15 — Decision 23: Approval materialization is tip-bound (superseded by Decision 28)

Category: System change

From an already committed Awaiting Approval ticket, trusted Linear materialization records Approved, the exact approval marker, and the materialized operator-field version in one single-ticket commit. Sequencing verifies that version and requires the commit at the remote tip; this is auditable bypass detection within the documented same-UID ceiling, not a cryptographic signature.

## 2026-07-13 — Decision 1: Repository-local decision numbering

Category: Decision

Software Factory decisions are numbered independently from Nysa product decisions. This keeps reusable tooling history separate from product history.

## 2026-07-13 — Decision 2: Family-typed Cursor fallback

Category: System change

Codex remains the production primary and Claude Code the checking primary. Optional Cursor adapters preserve those provider families and may be selected only by non-task probes before reservation and task submission; every post-submission failure stops without automatic retry.

## 2026-07-14 — Decision 3: Baseline v3 and durable documentation routing

Category: System change

Durable documentation was centralized under `docs/` and mechanically checked during PR readiness and CI. Plugin AI review is a pre-publication gate for changes to this kit; the factory's independent Reviewer, Narrator evidence bundle, and human approval remain authoritative for product tickets.

## 2026-07-14 — Decision 4: Exact-SHA product release isolation

Category: Decision

Products activate independently from sealed, read-only `releases/<full-sha>` trees under `~/.factory/kits`. External products fail closed without one exact full `KIT_PIN`; the implicit pin is limited to the in-kit conformance test bed, and ticket `Kit-SHA` affinity prevents silent mid-ticket upgrades.

## 2026-07-14 — Decision 5: Stable versioned Hermes boundary

Category: Decision

Hermes uses the bootstrap-managed `~/.factory/bin/factory-launch` contract instead of mutable checkout scripts. Contract `1.0.0` resolves and validates one physical release per invocation, exposes redacted public JSON and a read-only doctor, and treats unknown or error results as dispatch stops.

## 2026-07-14 — Decision 6: Fail-closed activation and rollback (superseded by Decision 10)

Category: Decision

Activation requires maintenance before the shared launch lock, an unexpired exact-tuple receipt, and a recoverable journal; rollback restores retained previous bits but keeps execution stopped until the protected product pin is reverted and revalidated. The acceptance targets are a control-plane outage of at most 5 minutes and full rollback RTO of at most 30 minutes. No automatic prune is allowed until real-ticket retention and rollback evidence exists.

## 2026-07-14 — Decision 7: Release checks have no bypass identity

Category: System change

The active `main` Ruleset has an empty bypass list and binds the required `ci` and `test-immutability` contexts to the GitHub Actions app (`integration_id` 15368). Release installation rejects bypass actors, unbound contexts, missing PR enforcement, or checks reported by another app.

## 2026-07-15 — Decision 8: Relay first migration accepted with measured exception

Category: System change

The isolated real-Hermes canary passed on release `45008d5`, and Relay generation 1 activated release `3b63cc7` before a hash-verified legacy restore and candidate recutover drill. The activation-to-clear interval was 5m50s, so the five-minute outage target remains unmet; formal `factory-kit rollback` begins with generation 2 because the legacy runtime had no previous activation record.

## 2026-07-15 — Decision 9: Parallel development, serialized production

Category: Decision

Partially superseded by Decision 223: isolated development remains valid, but
independently green commits may now share one composite successor and one
authoritative certification cycle.

Multiple focused kit branches and independently certified merged SHAs may progress concurrently, while each product pin change, activation, rollback, and live dispatcher ticket remains serialized and ticket-boundary gated. Relay T-107 proved the managed lifecycle on one Kit-SHA through planner, spec-linter, test-author, builder, reviewer, Narrator, approval, protected merge, production smoke, and Done closeout; the temporary PR #12 check override was restored immediately and is not accepted as protected evidence, while PRs #13 and #14 passed the restored required checks.

## 2026-07-15 — Decision 10: Protected product restore precedes rollback

Category: System change

Failed-cutover recovery keeps maintenance published while product factory services stop and any interrupted activation is reconciled. The protected previous full pin and product tree are restored before `factory-kit rollback`, which runs only when the candidate generation is committed and still active; health is revalidated before maintenance clears.

## 2026-07-15 — Decision 11: Relay generation 4 activated with a current-tree baseline

Category: System change

Relay generation 3 recertified current product tree `395918c` on retained release `3b63cc7`, then generation 4 activated documentation-only successor `35c2e10` through protected pin PR #15. Green draft revert PR #16 reproduces the baseline tree before rollback; health passed, but the 21m59s maintenance interval missed the five-minute target.

## 2026-07-15 — Decision 12: Bounded dispatcher concurrency candidate

Category: System change

Hermes contract 1.1 preserves one-ticket behavior by default and adds an opt-in maximum of two. Atomic opaque ticket leases gate sequencing and launches; they are never automatically stolen, budget reservations retain the global ledger lock, maintenance operations require a full drain, and stale recovery is an explicit operator action under maintenance.

## 2026-07-15 — Decision 13: Concurrency acceptance boundary

Category: Decision

The initial two-ticket pilot keeps one dispatcher, exact per-ticket branches and linked worktrees, sequential roles within each ticket, and sequential PR merges. Missing, stale, or mismatched leases fail before task submission; opaque lease IDs remain confined to trusted helpers; maintenance blocks claims and renewals while matching lease release remains available for drain; activation and rollback refuse until every lease is gone. A merge queue remains deferred until measured merge contention justifies it.

## 2026-07-16 — Decision 13: Fixed Python inside macOS release sandboxes

Release `71c17f2` passed protected CI but local production installation exposed a host-specific incompatibility: `/usr/bin/python3` invoked xcrun, whose cache write was correctly denied outside the Seatbelt workspace. The launcher now selects the fixed Command Line Tools Python binary when present, and install/certification sandboxes expose that same fixed interpreter through their trusted tool directory. Filesystem permissions remain default-deny; no external cache path is allowlisted.

## 2026-07-15 — Decision 14: Explicit primary role model routing

Category: System change

The factory now passes a fixed model and effort level to each primary role rather than inheriting CLI defaults. Cursor fallback remains family-matched and independently allowlisted.

## 2026-07-15 — Decision 15: Narrow operator and bookkeeping exceptions

Category: System change

Spec-linter now shares Reviewer's exact next-semantic-round operator authorization without adding a generic decision system. The immutability gate and reorder helper exempt only `.gitignore` and `context/memory.md` in addition to existing factory directories; `docs/` remains non-exempt, and protected reversals use `chore/<slug>-revert` branches.

## 2026-07-15 — Decision 16: Ticket worktrees are execution truth

Category: System change

Linear-owned priority, initiative, Ready, approval, and resume decisions live in the ignored sync overlay until the trusted `ticket-state` command materializes them. Preflight, sequencing, roles, and Linear projection use the exact ticket worktree or committed branch; successful mutating roles require a clean commit that the wrapper pushes and verifies, while Reviewer remains read-only.

## 2026-07-15 — Decision 17: Manifest-authoritative runtime accounting

Category: System change

Each run manifest records reservation, GO, terminal state, cost, and cost basis. Pre-GO failures cost zero; unknown post-GO cost retains the full reservation; consumers read the ignored effective runtime ledger, and only launcher-managed close-out projection updates tracked ledger history.

## 2026-07-15 — Decision 18: Reliability contract 1.2

Category: System change

Contract 1.2 binds preflight and sequencing to the exact ticket worktree, adds trusted ticket-state materialization and deterministic close-out ledger projection, and enforces role Git postconditions. The standalone launcher retains active 1.0 and 1.1 compatibility; 1.2 inherits 1.1 lease behavior without adding concurrency or supervision.

## 2026-07-15 — Decision 19: Certified destinations and terminal evidence gates

Category: System change

Automatic ticket and role pushes bind to the active generation's certified product origin and fail closed on remote drift. Generic state transitions cover role stages and escalation only; Awaiting Approval and Done remain unavailable until dedicated bundle and merge/deploy attestations can prove their prerequisites.

## 2026-07-15 — Decision 20: Runtime configuration is data only

Category: System change

`factory/ENVELOPE.env` and `~/.factory/global.env` use one sealed, Bash-3.2-compatible whitelist parser across preflight, adapter contracts, and role launch. Executable content, invalid or incoherent limits, and relative global-ledger paths fail before backend probes, run manifests, or task submission.

## 2026-07-15 — Decision 21: Provider-writable output paths are not accounting truth

Category: System change

The launcher durably publishes GO before opening the adapter gate, binds output to a wrapper-held descriptor, snapshots its manifests and registered checkout, and fails closed on persistent mutation. Runtime-ledger is output-only and is rebuilt from tracked durable history plus manifests read from a real no-follow root; missing or invalid telemetry retains the full reservation.

## 2026-07-15 — Decision 22: Runtime exclusion and machine accounting fail closed

Category: System change

Preflight durably initializes the ignored runs root. Ticket-and-role `mkdir` claims are never reclaimed by launch and cleanup removes only its exact owner; a configured global cap holds and validates its ledger across the complete provider interval, serializing globally capped runs and restoring an owned snapshot after mutation. These are portable integrity checks, not hostile same-UID isolation, which requires an OS boundary.

## 2026-07-19 — Decision 23: Claude preflight distinguishes liveness from contract drift

Category: System change

The sealed Claude readiness probe allows 30 seconds for local CLI startup. A timeout or nonzero help probe is unavailable, while successful help output missing a required flag is invalid and names that flag. Both states still fail pinned-route verification; the distinction prevents slow startup from being misreported as CLI contract drift.

## 2026-07-19 — Decision 24: Selector fixtures isolate the full-suite control flag

Category: System change

The selective-CI self-test clears the outer `CI_FORCE_FULL` controller flag for ordinary fixture cases and retains a separate explicit force-full assertion. Protected-main full verification can therefore exercise selector behavior without forcing every nested fixture to report the controller mode.

## 2026-07-21 — Decision 25: In-flight release refresh remains evidence-bound

Category: System change

A protected-main authorization may bridge only exact named old-kit ticket branch heads across an exceptional release activation; maintenance, active-run, and dispatcher-lease drain barriers remain unchanged. After sealed route migration, a non-force base refresh retires old bundle and approval receipts and records role/verdict baselines so fresh Reviewer, Narrator, bundle, and Linear approval evidence is mandatory. One authentic superseded Contract 1.2 Planner manifest is retained only as digest-bound legacy accounting evidence, never as modern route authority.

## 2026-07-21 — Decision 26: Isolated development lane validated with Cursor

Category: System change

The reusable development lane runs a synthetic six-role lifecycle from committed factory source against a disposable local product and origin, without release installation, registration, activation, or canonical Nysa state. Mock verification remains under 15 minutes; real Cursor requires a one-use, content-bound approval. A complete real lifecycle reached `AWAIT-OPERATOR` with Cursor OpenAI for Planner, Builder, and Narrator, Cursor Fable for Spec-linter and Test-author, and Cursor Sonnet for read-only Reviewer. The lane reserves collision-free fixture ports, decodes the validated Cursor terminal result before verdict parsing, and retries one transient authentication or model-list miss while still failing closed after two misses.
- Development Narrators may immediately explain a `Not applicable — backend-only contract` Preview or Screenshots marker; validation recognizes the marker at the start of the section text so valid evidence is not replayed for punctuation.
## 2026-07-25 — Decision 27: Checkpoints use one branch authority

Category: System change

Development checkpoint evidence resolves ticket heads from the same lane-local
bare origin used to build the seed bundle. Worktree remote-tracking refs are
non-authoritative caches; stale values cannot detach checkpoint metadata from
the exported commits.

## 2026-07-25 — Decision 28: Resume refreshes credentials and exact role phase

Category: System change

A drained development resume refreshes its isolated native Codex and Claude
credential copies before computing the one-use approval. Blocking transitions overwrite
`Resume-State` with the actual current phase, and the scheduler materializes
that phase for any role rather than assuming Planner.

## 2026-07-25 — Decision 29: Matching Cursor callbacks are one verdict

Category: System change

Cursor may attach a late background-check notice to an otherwise canonical
Reviewer result. The shared parser accepts the observed bold summary only when
its verdict and repair owner exactly match the preceding corrupted callback;
ambiguity and contradiction remain refusals.

## 2026-07-25 — Decision 30: Claude setup tokens stay lane-local

Category: System change

When native Claude stores subscription authentication only in the macOS
Keychain, the development controller may materialize an owner-only long-lived
setup token into the existing isolated credential file. The token source,
derived expiry, route, and one-use approval are bound together; API keys,
shared Keychain access, and production credentials remain unavailable.
Claude 2.1.209 also requires read-only `/dev/dtracehelper` access at startup;
the native Seatbelt profile grants only that exact read while retaining its
default-deny filesystem boundary.
Readiness resolves and pins that executable's exact semantic version before
the shared backend probe, avoiding a stale default while preserving
approval-bound version and executable drift checks.
macOS refuses Claude's inner `sandbox_apply` when the CLI already runs beneath
the Factory Seatbelt. The development lane therefore keeps the Factory-owned
outer Seatbelt authoritative and disables only Claude's redundant inner
Seatbelt; attempt-local homes, configuration, temporary directories,
credentials, process groups, and cleanup remain mandatory.

## 2026-07-25 — Decision 31: Failed durable output is retained, not promoted

Category: System change

A provider budget failure can occur after a mutating role commits clean output.
Explicit development resume now binds that exact failed manifest and linear
commit history into an owner-only diagnostic receipt and ref, restores the
unchanged isolated-origin tip, and reruns only the failed role. It never
rewrites accounting or treats the failed provider result as successful.

## 2026-07-26 — Decision 32: Pre-GO resume drift invalidates its approval

Category: System change

If a retained development resume changes after approval but before provider GO,
the controller archives the exact unused approval and resume basis, removes the
approval, restores the original ticket selection, and stops without a provider
attempt. The existing checkpoint-export and v5 lineage path remains the sole
cross-budget-day recovery mechanism.

## 2026-07-25 — Decision 33: T-072 is canceled with diagnostics retained

Category: Context

The operator canceled T-072 before publication. Its final branch head,
successful Cursor Test-author output, Codex contract-blocked Builder output,
route plan, timing, and run metadata are retained under the owner-only
`rolling-mvp-t072-cancelled-20260726T0115Z` checkpoint; no implementation PR
exists, and the three retained disposable lanes were cleaned. A future
replacement may allow only its focused fixture to seed the existing
workspace-scoped `decision` category, without changing product schema,
migration, or persistence behavior.

## 2026-07-27 — Decision 34: Cursor qualification capacity is four

Category: System change

The real-provider concurrency proof permits four isolated Cursor subscription
calls under the existing global ceiling. Development product lanes and
activation validation use that same four-call limit; per-attempt homes,
configuration, data, temporary files, credentials, process groups, and trusted
terminalization remain mandatory.

## 2026-07-27 — Decision 35: Tickets are portable controller-owned work

Category: System change

Sandbox tickets are not durable children of an execution lane. Git-authored
state and a v2 ticket passport own lifecycle continuity; a lane is a
replaceable execution cell. The deterministic controller issues ticket-scoped
approvals, hard-gates protected dependencies, runs at most four workers, and
refills released capacity without waiting for sibling drain. Per-ticket and
aggregate budgets remain the delivery limits. Ticket wall-clock, attempt, Spec,
and Reviewer round caps are removed; provider-call timeout and exact
no-progress retry refusal remain safety controls. Systemic failures are tracked
in `docs/evidence/software-factory-improvement-log.md`.

## 2026-07-27 — Decision 36: Fresh review and cancellation recovery are explicit

Category: System change

Reviewer reconciliation passes checkpoint evidence only when a real import
exists. Authenticated cancelled-role recovery accepts shell status 130 and
macOS SIGTERM status 143 while retaining every existing manifest, head,
remote, GO, and accounting check; partial output is snapshotted before the
trusted worktree is restored.

## 2026-07-27 — Decision 37: Optional Reviewer checkpoints use explicit branches

Category: System change

The trusted ticket-state helper invokes Reviewer reconciliation with
`--checkpoint` only when an authenticated checkpoint exists. It does not encode
an absent optional argument as an empty array, preserving checkpoint-free
recovery on the supported macOS Bash 3.2 host.

## 2026-07-27 — Decision 38: Contract repair stages map to the named role

Category: System change

The development controller accepts only explicit `FIX planner`, `FIX
spec-linter`, `FIX test-author`, or `FIX builder` stages emitted by the
authenticated repair parser. It maps each stage directly to that role and
continues to refuse ambiguous or unsupported repair ownership.

## 2026-07-27 — Decision 39: Passport Spec evidence preserves Markdown indentation

Category: System change

Checkpoint export and replay both accept canonical Spec-lint markers with
normal leading Markdown whitespace, strip that whitespace, and compare the
ordered verdict prefix exactly. Altered verdict text and unmatched current-lane
evidence still fail closed.

## 2026-07-27 — Decision 40: Ticket publication follows ticket approval

Category: System change

A selected-ticket export may proceed while the compatibility batch approval
remains unused and inert. Batch export retains the unused-approval refusal;
ticket export keeps every existing terminal-role, drain, branch, reviewed-head,
and patch-history gate.

## 2026-07-27 — Decision 41: Retained failures bind to their executing Factory

Category: System change

Qualification membership remains protected-product state, but an authenticated
contract-blocked role is validated against the immutable Factory checkout that
executed the lane. A later successor-candidate selection cannot invalidate an
older unaffected ticket's pinned failure evidence; a manifest from any other
Factory SHA still fails closed.

## 2026-07-27 — Decision 42: Reviewer callbacks have one canonical verdict

Category: System change

The trusted Reviewer parser normalizes Cursor's named background-shell callback
only when a later summary repeats the same Request-changes verdict and repair
owner. The role wrapper and reconciliation therefore consume the same
authenticated result; contradictory or ownerless callbacks still fail closed.

## 2026-07-27 — Decision 43: Ticket-only operator notes preserve review

Category: System change

Unmatched Reviewer evidence may reconcile at a descendant head only when every
change since the reviewed head is confined to that ticket's Markdown document.
The reviewed local and remote heads must still match exactly. Product, test,
configuration, or unrelated-document drift invalidates the evidence.

## 2026-07-27 — Decision 44: Protected CI reopens only the owning repair

Category: System change

An authenticated operator-await checkpoint may resume after protected GitHub
CI fails only when the ticket records one exact Actions job URL and names
Builder or Test-author with `OPERATOR PUBLICATION REPAIR`. The checkpoint keeps
all imported successful roles; the named repair runs once, then fresh Reviewer
and Narrator evidence is required before operator-await can recur.

## 2026-07-27 — Decision 45: Zero attempts reduce to an empty ledger

Category: System change

A failed pre-submission product plan may consume authenticated seed accounting
without creating a runtime ledger. Checkpoint re-export treats that absent
ledger as zero local attempts, preserving the consumed lineage and every
imported role; an existing ledger retains its exact validation.

## 2026-07-27 — Decision 63: Ticket checkpoints outlive product bases

Category: System change

A portable operator-await checkpoint may cross a newer protected product base
only when a live conflicting PR proves the exact sealed product patch and both
qualification bases add control paths only. Replay preserves protected-main
content at safe Builder-owned conflicts and records exact blobs before Builder,
fresh Reviewer, and Narrator continue; every other conflict fails closed.

## 2026-07-27 — Decision 64: Contract 1.8 has one deterministic controller

Category: System change

The release-owned one-shot controller is the only ticket router. It claims up
to four passport-owned tickets, obtains one-use state-machine receipts, records
restart/relocation/publication events, and serializes only merge publication.
The exact candidate remains unqualified until the four-ticket reducer matches
authenticated role and charge evidence to protected GitHub merge truth.

## 2026-07-27 — Decision 65: Contract 1.8 preflight is a one-time kickoff gate

Category: System change

The deterministic controller runs preflight only on the first Planner receipt,
where the state machine has already moved the ticket to Planning. Spec-linter,
Test-author, Builder, Reviewer, and Narrator continue from authenticated role
evidence without repeating route-wide kickoff probes.

## 2026-07-27 — Decision 66: Pre-provider branch reset is protected and non-force

Category: System change

A successor qualification may reconcile an exact authorized remote branch
whose history contains only Factory pin and Ready-to-Planning controls. The
controller validates the unchanged ticket contract, non-force merges current
protected main, records the old head, and removes only obsolete control state;
unlisted heads and provider-authored drift remain blocked.

## 2026-07-27 — Decision 67: Qualification roots are provider execution environments

Category: System change

The sealed qualification launcher preserves its already-validated owner-only
root as the isolated subscription execution environment. The generated marker
authorizes per-attempt runtime homes under that root; tickets remain bound to
their passport and branch, not to any disposable cell path.

## 2026-07-27 — Decision 68: Canonical pre-provider recovery is repeatable

Category: System change

An exact authorized ticket branch may cross more than one protected-main or
Factory successor before its first provider GO. Every first-parent commit must
follow the Factory's pin, transition, recovery-merge, supersede, and repin
grammar; every merged protected parent must remain in current main lineage.

## 2026-07-27 — Decision 69: Qualification rejects unusable provider scratch roots

Category: System change

Sealed qualification preparation calculates a conservative Cursor attempt data
path before creating release state. A root that exceeds the adapter's existing
isolated-scratch limit fails before ticket admission, reservation, or role
execution.

## 2026-07-27 — Decision 70: Receipt identity stays host-only through GO

Category: System change

The role runner captures the launcher-validated project as a non-exported host
binding before clearing provider-facing model controls. Initial, locked, and
pre-GO receipt checks use that binding; provider processes still inherit
neither `FACTORY_PROJECT` nor `FACTORY_MODEL_STATE_ROOT`.

## 2026-07-27 — Decision 71: Qualification provider failures route once

Category: System change

The deterministic controller retains a qualification ticket after its first
terminal Cursor provider failure and invokes the existing same-family
direct-CLI fallback for the same stage. The fallback commit is restart-safe;
a second task-submitted attempt for that role remains the duplicate/no-progress
stop.

## 2026-07-27 — Decision 72: Cancellation previews bind stable snapshots

Category: System change

Separate launcher plan and apply invocations derive the same cancellation
preview from the exact run manifest, PID record, and reason. Any intervening
attempt mutation changes the preview hash and preserves the existing CAS
refusal.

## 2026-07-27 — Decision 73: First qualification fallback creates its journal

Category: System change

When a protected qualification ticket still carries its initial v1 route
plan, the first eligible Cursor failure preserves that exact plan as revision
zero of a same-release v2 journal and appends the direct-CLI fallback in the
same handoff commit. Generic operator fallback continues to require a
pre-existing v2 journal.

## 2026-07-27 — Decision 74: Unreported required checks are pending

Category: System change

Immediately after an exact ticket PR is created, GitHub may report that no
required checks exist before Actions publishes their runs. The ticket PR gate
treats only that exact empty CLI response as wait; malformed or unrelated
GitHub responses still fail closed.

## 2026-07-27 — Decision 75: Remote observation gets one transport retry

Category: System change

A successful mutating role retains its clean local commit while the trusted
wrapper retries one failed exact-head remote observation. A second transport
failure or any observed head drift still fails closed before push.

## 2026-07-27 — Decision 76: Invalid review prose does not complete Reviewer

Category: System change

The shared Reviewer parser accepts the role contract's standalone verdict and
exact Markdown `Verdict:` heading. The role wrapper validates the durable
terminal output before recording success; malformed output retains its charge
but not completed-role evidence, and the controller reruns only Reviewer under
the remaining ticket budget.

## 2026-07-27 — Decision 77: Model pinning has no aggregate delivery timeout

Category: System change

Each model readiness probe remains individually bounded and fail-closed.
The controller does not add a second aggregate timeout around the six-role
pinning transaction, so slow successful probes cannot block tickets before
provider submission merely because their combined duration exceeds five
minutes.

## 2026-07-27 — Decision 78: Machine readiness is probed serially

Category: System change

Contract 1.8 serializes only ticket model-plan pinning inside one controller
reconciliation. This prevents four identical local CLI readiness probe sets
from starving one another while preserving four-way concurrency for
task-bearing roles and protected PR validation.

## 2026-07-27 — Decision 79: Temporary model readiness preserves claims

Category: System change

A model plan containing only ready and temporarily unavailable route evidence
is a shared controller wait, not a ticket failure. One serialized probe pauses
the four-ticket cohort without releasing claims; invalid or unknown route
evidence remains a fail-closed configuration error.

## 2026-07-28 — Decision 80: Readiness resolves before concurrent ticket work

Category: System change

The Contract 1.8 controller resolves one task-free model plan before opening
the concurrent ticket worker pool and pins every route-less clean branch from
that in-process batch resolution. Release validation and per-adapter timeouts
remain unchanged; only task-bearing roles and protected PR validation fan out
four ways.

## 2026-07-28 — Decision 81: Controller readiness uses interactive launchd QoS

Category: System change

The Contract 1.8 controller LaunchAgent uses `ProcessType=Interactive`.
macOS background QoS can exhaust otherwise healthy bounded local CLI
readiness probes; their timeout, validation, and fail-closed behavior remain
unchanged.

## 2026-07-28 — Decision 82: Planner preflight is provider-free

Category: System change

After the controller authenticates and pins the shared model plan, Planner
preflight validates all six route selections structurally and does not repeat
credential-bearing readiness. The role runner still re-probes its one selected
route immediately before provider admission and fails closed on drift.

## 2026-07-28 — Decision 83: Automatic fallback is a serialized transition

Category: System change

Contract 1.8 serializes only the task-free `fallback-auto` readiness and
route-journal mutation inside one reconciliation. Provider-bearing roles and
protected PR validation remain four-way; independent fallback tickets resume
in deterministic controller order without competing for the product launch
lock.

## 2026-07-28 — Decision 84: Qualification fallback changes one role

Category: System change

Automatic qualification fallback resolves only the exact failed role.
Unstarted roles retain their pinned selections and are checked when they reach
provider admission, so an unrelated future route outage cannot block a valid
same-stage recovery.

## 2026-07-28 — Decision 85: Protected-base Git mutation is serialized

Category: System change

Contract 1.8 keeps protected PR validation concurrent but serializes
protected-base refresh and closeout fetches because disposable cells share one
Git common directory. Exact certified remote-tip, ancestry, and open-PR
identity prove refresh eligibility; GitHub `mergeStateStatus` may lag and is
not authority.

## 2026-07-28 — Decision 86: Release identity cannot shadow lifecycle actions

Category: System change

The controller stores its immutable release directory as `release_path`.
Ticket completion and cancellation retain the distinct `release` lifecycle
action, and focused coverage must prove a completed claim is actually removed.

## 2026-07-28 — Decision 87: Qualification upgrades preserve one controller root

Category: System change

A proven mid-qualification Factory defect advances the sealed release only
through the qualification preparer's drained, lock-protected upgrade action.
The controller directory, passport authentication key, passports, claims, and
provider ledger remain in place; fresh-root secret copying and active-record
edits are forbidden.

## 2026-07-28 — Decision 88: Cross-release recovery rebinds exact blocked claims

Category: System change

A blocked claim may reacquire a dispatcher lease only when authenticated
passport migration proves it belongs to a prior Factory release and current
ticket lineage. The controller saves the new exact-ticket lease before
migration and returns the claim to `claimed` only after migration succeeds;
same-release blockers never reopen automatically.

## 2026-07-28 — Decision 89: Protected remote reads get one exact retry

Category: System change

Protected attestation retries a failed read-only `git ls-remote` once with
identical arguments. A second transport failure, every Git mutation failure,
and every semantic or identity mismatch still fail closed.

## 2026-07-28 — Decision 90: Upgrade liveness uses runtime authority

Category: System change

Qualification upgrade liveness is proven by the non-overlapping controller
lock and active-run markers, not a stale claim status. Terminal orphan claims
remain authenticated successor-recovery input instead of blocking cutover.

## 2026-07-28 — Decision 91: Reviewer formatting preserves explicit semantics

Category: System change

Exact verdict-only Markdown headings, exact Markdown-wrapped repair owners, and
known Cursor background-callback concatenation are normalized only when every
verdict and owner signal agrees. This supersedes Decision 42's requirement for
one particular later-summary sentence; ambiguity, contradiction, or a missing
owner still fails closed.

## 2026-07-28 — Decision 92: Push repair reopens only the failed role

Category: System change

A blocked `role_exit_push_failed` may re-enter the state machine only after its
signed passport validates the clean cell and the passport head exactly equals
the remote ticket tip. The controller rebinds that ticket's lease and clears
only the failed receipt; every successful role and every other blocked failure
remain unchanged.

## 2026-07-28 — Decision 93: Exact refresh-topology refusal is repairable

Category: System change

The deterministic resolver may receipt one exact single-line `REFUSE` emitted
with exit 1 and empty stderr. Only the named direct-after-merge refresh-topology
refusal routes to authenticated protected-base refresh; malformed, forged,
baseline-mismatched, unknown, or multi-line refusals remain blocked.

## 2026-07-28 — Decision 94: Failed-role refresh uses the refusal receipt

Category: System change

Protected-base refresh may reset a `Building` ticket to `Review` only when the
trusted launcher supplies the exact consumed receipt stage for the named
direct-after-merge topology refusal. State alone, an unverified environment
value, and every other receipt remain insufficient.

## 2026-07-28 — Decision 95: Pre-submission interruption reopens only its role

Category: System change

An abandoned action may re-enter the state machine only when its immutable
terminal manifest proves exit 143 before provider submission with no role
output, and its signed passport, clean cell, branch, and remote head still
agree exactly. The controller clears only that interrupted receipt; submitted
or differently terminated actions remain blocked.

## 2026-07-28 — Decision 96: Protected authorization can bind a passport rewrite

Category: System change

A cross-release passport migration may cross non-ancestral ticket heads only
when protected main exactly authorizes the repository, source/target kits,
ticket, branch, new head, and state, the cell is clean, and the authenticated
route digest is unchanged. Same-release and every partially matching rewrite
remain blocked; invalid ancestry is never reattached merely to satisfy Git.

## 2026-07-28 — Decision 97: Route migration precedes passport recovery

Category: System change

Cross-release recovery must retain the blocked claim and prior passport until
the ticket and route journal both name the successor Factory SHA. A typed
migration-required event is the only intermediate action; the existing
authenticated passport migration and exact-claim recovery run afterward.

## 2026-07-28 — Decision 98: Budget authority includes ticket-cap overrides

Category: System change

Contract 1.8 ticket budget stops use the authenticated base envelope plus the
active ticket-scoped cap override, matching role admission. A budget-wait
claim binds the envelope and immutable override records so an authorized cap
change re-enters deterministic reconciliation without manual state edits.
Malformed, conflicting, expired, and unrelated overrides remain fail-closed.

## 2026-07-28 — Decision 99: Both exact refresh-topology refusals self-repair

Category: System change

An exact stale-ancestry receipt and an exact receipt-not-directly-after-merge
refusal both authorize only the existing receipt-bound protected-base refresh.
The stale receipt is never accepted as evidence. `Building` refresh remains
forbidden unless the trusted transition stage equals one of those two typed
topology refusals.

## 2026-07-28 — Decision 100: Exported terminal checkpoints are idempotent

Category: System change

When a controller stops after passport export but before clearing the running
claim, restart requires the exact terminal run, role, and receipt in the
passport charge evidence and, for success, completed-role evidence. It
authenticates and migrates that passport, then finishes reconciliation without
re-exporting or replaying the role. Any partial or mismatched checkpoint stays
blocked.

## 2026-07-28 — Decision 101: Admission cannot block retained checkpoints

Category: System change

Budget reopening reacquires the exact ticket lease on the retained claim rather
than deleting it and returning through fresh admission. A claim missing because
of an older controller may be reconstructed only from one signed nonterminal
passport, its exact cell/branch, current ticket and route Kit-SHAs, and a new
lease. Any new-admission refusal pauses admission while already authenticated
claims continue through the state machine.

## 2026-07-28 — Decision 102: Route schema migration need not change releases

Category: System change

The first v2 route-journal revision authenticates conversion of the exact
embedded v1 plan and may retain its Kit-SHA. Only a later
`release-migration` revision asserts a release change. Ticket attestation still
requires the exact legacy bytes, digest, policy, selections, pin commit, Kit
identities, and complete parent-hashed journal chain.

## 2026-07-28 — Decision 103: Rewrite authentication precedes route migration

Category: System change

For a blocked cross-release claim, the controller may migrate the signed
passport to the successor while the old route digest is still exact, but it
keeps the claim blocked and records a durable pending marker. The
preview-approved route migration runs next; a second descendant passport
migration binds its new route digest before the claim reopens. This supersedes
Decision 97 only for passport authentication order, not for execution: no
ticket runs against a mismatched route or Kit-SHA.

## 2026-07-28 — Decision 104: Review validity follows the exact semantic base delta

Category: System change

A protected-base refresh preserves successful Reviewer and Narrator evidence
only when one shared fail-closed classifier proves the immutable base delta is
limited to modified regular `factory/KIT_PIN` and
`factory/QUALIFICATION.json` blobs plus added regular authenticated
in-flight-release records. The ticket head must retain those exact protected
blobs. Application code, tests, contracts, CI, configuration, renames, type
changes, deletions, and every unknown path continue to invalidate review.

## 2026-07-28 — Decision 105: Budget increases supersede evidence; they do not erase it

Category: System change

A preview-bound persistent envelope override may replace exactly one active
record only when it names that record, retains the same scope, target,
base-envelope identity, and setting keys, is issued later, and expires no
earlier. The predecessor remains immutable and authenticated. Missing,
ambiguous, shortened, differently scoped, or differently keyed replacements
fail closed; one-use next-attempt records retain consumption semantics.

## 2026-07-28 — Decision 106: Refusal cannot cross the passport boundary

Category: System change

A deterministic `REFUSE` transition binds its exact receipt but leaves the
passport unchanged. The controller blocks the claim first and alone performs
authenticated cross-release migration plus the restart-safe pending marker;
non-refusal transitions retain ordinary passport migration.

## 2026-07-28 — Decision 107: Qualification closeout follows authenticated lineage

Category: System change

An operator-approved Contract 1.8 closeout targets exactly three or four
tickets while retaining capacity-four restart and relocation proof. The
controller leaves excluded claims untouched. The reducer accepts historical
roles, charges, and merges only through each signed passport's Factory history,
uses the sealed active ticket-cap override chain, retains the `$100` cohort
ceiling, and refuses any Factory change after the final candidate starts.

## 2026-07-28 — Decision 108: Publication honors authenticated semantic refresh

Category: System change

Contract 1.8 ticket-PR validation may carry successful Reviewer and Narrator
evidence past a changed branch SHA only when the trusted transition receipt,
direct-after-merge refresh topology, shared non-semantic base classifier, and
exact retained protected-base control blobs all validate. Route migration
remains independently append-only; application, test, contract, CI,
configuration, unknown, renamed, deleted, typed, malformed, and stale inputs
still fail closed.

## 2026-07-28 — Decision 109: Semantic refresh cannot preserve orphaned role evidence

Category: System change

The narrow control-only protected-base allowlist preserves the latest effective
Reviewer and its later effective Narrator only when their manifest heads belong
to the receipt-bound old ticket head. Earlier successful runs on a discarded
force-pushed lineage remain immutable accounting evidence but do not disqualify
a later valid pair. An orphaned Reviewer reruns Reviewer and downstream
Narrator; an invalid Narrator reruns only Narrator. Sequencing and bundle
attestation enforce the same rule.

## 2026-07-28 — Decision 110: Publication readiness is current state

Category: System change

Contract 1.8 removes a ticket's lease-free publication queue record whenever
deterministic reconciliation no longer classifies it as merge-ready. This
prevents an older failed or approval-revoked ticket from blocking a later
independent green PR while preserving capability-bound release for an active
lease and deterministic priority ordering among tickets that remain ready.

## 2026-07-28 — Decision 111: Publication retries one exact remote observation

Category: System change

The Contract 1.8 ticket-PR boundary retries its exact read-only branch-head
observation once after a transport failure, matching the existing role-runner
and attestation boundary. A second transport failure, different head, semantic
refusal, or any mutating operation still fails closed.

## 2026-07-28 — Decision 112: Excluded claims cannot remain publication-ready

Category: System change

Contract 1.8 qualification leaves excluded claims, passports, roles, and
charges parked, but withdraws their lease-free publication queue records before
filtering the active cohort. An excluded ticket therefore cannot hold
checkpoint head-of-line priority over selected independent work.

## 2026-07-28 — Decision 113: Ticket rewrite authorization is lane- and release-neutral

Category: System change

A same-release non-ancestral Test-author repair no longer requires an unrelated
Factory release cut. One exact protected record must bind the signed old
passport, consumed repair receipt, typed failed non-force push, old/new ticket
heads, unchanged route, and a clean final-tree delta limited to configured
tests plus the ticket log. The failed attempt remains charged but unsuccessful.
The controller never force-pushes and reopens only Test-author after the exact
operator-authorized head is observed remotely.

## 2026-07-28 — Decision 114: Continuous-improvement sessions preserve the qualified candidate

Category: Decision

The reusable Factory improvement-session prompt lives at
`docs/runbooks/factory-continuous-improvement-session-prompt.md`. It requires
read-only evidence reconciliation first, a separate improvement branch from
any frozen promotion candidate, focused local checks with the full regression
left to protected GitHub CI, mode-aware improvement-log updates, and separate
non-inheriting authorization for implementation, qualification, promotion,
sealing, Relay cutover/rollback/recutover, and Nysa activation.

## 2026-07-28 — Protected promotion CI caught a stale Contract 1.2 fixture

Category: Context

The first protected-main run for Factory 1.8 correctly refused a valid
`models pin-batch` test executed through a Contract 1.2 release on both Linux
and macOS. The release remains unsealed until a separate Contract 1.8 batch
fixture passes protected GitHub CI; the launcher guard is unchanged.

## 2026-07-28 — Decision 115: Launcher-boundary fixtures exclude internal batch calls

Category: System change

The Hermes model helper fixture records every external launcher invocation but
does not let `pin-batch`'s internal `pin` subprocess overwrite that snapshot
after trusted machine configuration has loaded. Caller-control confinement
remains fail closed at the actual launcher boundary.

## 2026-07-28 — Decision 116: Contract grammar assertions track batch pin

Category: System change

The exact Hermes public-command assertion includes Contract 1.8
`models pin-batch` alongside its launcher grammar. Static contract drift is
checked directly without weakening runtime validation.

## 2026-07-28 — Decision 117: Sealed shell helpers never write Python bytecode

Category: System change

The ticket-state boundary disables Python bytecode writes before importing any
release-local module. A deterministic state transition therefore cannot mutate
the sealed Factory tree it is validating.

## 2026-07-28 — Decision 118: Contract 1.8 fixtures carry authenticated outputs

Category: System change

Successful role fixtures include an output artifact and its exact SHA-256, and
protected-main bare remotes advertise their real `main` branch. Budget and
passport tests now exercise valid authenticated evidence instead of failing
before the behavior under test.

## 2026-07-28 — Decision 119: Host-specific qualification checks are explicit

Category: System change

macOS `libproc` and `/private/tmp` qualification-root checks run only on macOS;
portable receipt validation uses secure Python metadata reads. Focused
development-lane tests load every direct helper dependency they execute.

## 2026-07-28 — Decision 120: Successful Narrator fixtures include their bundle

Category: System change

A successful Narrator ledger row is not sufficient evidence by itself.
Checkpoint-recovery fixtures create the required evidence bundle so the
fail-closed state machine still retries or escalates genuinely missing output.

## 2026-07-28 — Decision 121: Provider contract binds after kit validation

Category: Superseded

The role runner snapshots its effective provider contract immediately after
kit validation. Mutable development lanes therefore retain concurrent provider
admission, while sealed launches continue to use provenance-validated release
metadata.

## 2026-07-29 — Decision 122: Provider contract binds from launcher metadata

Category: System change

Decision 121 over-bound mutable direct fixtures to the kit manifest's Contract
1.8 path. The role runner instead snapshots the caller's sealed-release,
explicit, or trusted development contract before validation. Sealed Contract
1.8 execution remains receipt-only, while explicit Contract 1.7 development
lanes retain concurrent admission without changing unrelated harnesses.

## 2026-07-29 — Decision 123: Development trust metadata is host-neutral

Category: System change

Development-lane security checks obtain numeric ownership, octal permissions,
and link counts through Python `lstat` rather than host-specific `stat`
formats. The same fail-closed expectations now run on Linux and macOS.

## 2026-07-29 — Decision 124: Ordering fixtures create clock ties explicitly

Category: System change

Publication ordering tests persist the exact same `publication_ready_at` for
tickets whose ticket-ID tie-break is under test. Production continues to order
by priority, actual ready time, then ticket ID.

## 2026-07-29 — Decision 125: Pipefail assertions consume producer output

Category: System change

Static shell assertions under `pipefail` do not use an early-exiting
`grep -q` downstream of a potentially long producer. They consume the full
stream and discard output so GNU and BSD pipeline behavior remains equivalent.

## 2026-07-29 — Decision 126: Test fixtures use portable in-place edits

Category: System change

Development-lane fixtures use `sed -i.bak` plus explicit backup removal for
in-place edits. No BSD-only `sed -i ''` invocation remains in the Factory.

## 2026-07-29 — Decision 127: Restricted process identity is host-neutral

Category: System change

The restricted sandbox `ps` helper retains its narrow command surface and uses
the host-native process source: macOS `libproc` or Linux `/proc`. Linux identity
uses raw process-start ticks rather than wall-clock reconstruction, so clock
adjustment cannot invalidate a live lock. Protected Linux mock lanes can bind
launch and provider locks without weakening PID-reuse protection.

## 2026-07-29 — Decision 128: Exact terminal evidence survives rollback recutover

Category: System change

Normal Done validation identifies the unique original closeout by its
authenticated parent topology and exact current blob instead of requiring the
path to have been added only once in protected history. A protected rollback
and byte-identical recutover therefore preserve evidence, while changed or
multiple matching closeouts still fail closed.

## 2026-07-29 — Decision 129: Linear soft wraps do not create projection drift

Category: System change

Linear inserts a three-space continuation wrap inside long ordered-list
paragraphs. Reconciliation now normalizes only that semantically inert form
when comparing descriptions, while nested list markers and exact Git contract
bytes remain significant.

## 2026-07-29 — Decision 130: Same-release contract resume is receipt-bound

Category: System change

Contract 1.8 moves a durable Planner, Test-author, or Builder contract blocker
to `Blocked-Escalated` only when its consumed transition receipt and unique
terminal manifest agree. The controller releases its lease and waits for
Linear to select the exact recorded resume state. The state machine then
migrates the signed passport and reclaims only that ticket, preserving prior
role evidence, charges, and sibling execution.

## 2026-07-29 — Decision 131: Derived ledger staging stays inside runtime

Category: System change

Concurrent runtime-ledger refresh stages its atomic temporary inside the
existing ignored real `factory/runs/` directory before renaming it to
`factory/runtime-ledger.csv`. The registered-checkout mutation sentinel
therefore remains strict without treating launcher-owned atomic intermediates
as provider mutations.

## 2026-07-29 — Decision 132: Read-only activation planning propagates refusal

Category: System change

Activation planning runs inside a command substitution, where Bash 3 clears
`errexit`. The plan function therefore explicitly returns any ticket-lease
validation failure before emitting a plan tuple. Planning and activation keep
the same fail-closed ticket, passport, protected-head, and release checks.

## 2026-07-29 — Decision 133: Recovery progress is ticket-scoped

Category: System change

A same-release contract blocker may name one exact repair owner in an
otherwise unchanged ticket commit. Passport lineage authenticates the
directive and an HMAC-bound repair record keeps only that owner and
deterministically required downstream roles active. Coarse ticket states do
not gain a general backward transition, successful roles are not replayed, and
duplicate owner success fails closed. Controller workers advance each ticket
until its own real wait instead of waiting for a slow sibling's checkpoint,
and dispatch leases heartbeat before provider admission queues.

Cursor's configured timeout is an inactivity window. Only structured events
normalized by the trusted stream parser extend it, an absolute two-window hard
limit remains, and malformed or rewritten progress terminates the run fail
closed.

## 2026-07-29 — Decision 134: Product certification is a measured bounded DAG

Category: System change

The Factory may execute a repository-owned certification plan with up to three
workers, initially two. Every phase records wall time, CPU, peak memory, cache
status, input and artifact digests, separate logs, and a separate temporary
directory. The first failure cancels sibling process groups and prevents a
passing receipt. Passing evidence binds the exact Factory SHA and product tree
inside the certification receipt. Protected Factory CI evidence continues to
be reused, and build/test-result caching remains disabled until measurements
justify an exact-key policy.

## 2026-07-29 — Decision 135: Failed certification output reaches redaction

Category: System change

The measured certification runner emits the first failed phase's isolated log
only after terminal accounting. The outer Factory capture still redacts it
before operator display. Canceled sibling logs remain isolated, and no passing
receipt is issued. This preserves exact-boundary diagnosis without weakening
phase isolation or exposing raw output through the Factory command.

## 2026-07-29 — Decision 136: Contract repair survives a Factory cutover

Category: System change

A blocked Planner, Test-author, or Builder receipt remains historical evidence
when its ticket moves to a successor release. The state machine accepts it only
when the current HMAC-authenticated passport orders the old and new releases,
binds the exact charge and terminal manifest, retains the old head in current
ancestry, contains no successful evidence for that receipt, and a live
exact-ticket lease belongs to the successor claim. The successor controller
preserves those blocked fields or deterministically reconstructs fields cleared
by an earlier controller from the latest exact checkpoint. No successful role
is replayed and no historical charge is duplicated.

## 2026-07-29 — Decision 137: Multi-ticket activation requires provider concurrency

Category: System change

Contract 1.8 capacity above one is not ready when only ticket cells are
concurrent. A canonical approval-hash-bound owner-local policy must cover
Cursor, Claude Code, and Codex at ticket capacity, and every call uses a
private home/config/tmp root plus authentication copy. Doctor, certification,
activation, and role pre-admission refuse missing or drifted state. Legacy
serialization remains only for older contracts, explicit capacity one, and
non-activated legacy routes.

## 2026-07-29 — Decision 138: Legacy merged dependencies need explicit fulfillment

Category: System change

Strict pre-provider dependency gating exposed Nysa T-040 and T-092: their
application PRs are on protected main, but their pre-Contract-1.8 Backlog
tickets have no terminal receipt. A one-time dependency-only migration now
binds their exact merged PRs, successful required checks, protected basis,
source ticket blobs, operator authorization, and target Factory release in the
same manual control commit. The receipt satisfies only dependency readiness;
terminal projection, broad inference from Git history, and fallback around
partial terminal evidence remain forbidden.

## 2026-07-29 — Decision 139: Release checks stay portable across protected runners

Category: Incident

Protected Linux CI found a Bash parse boundary that macOS system Bash had
accepted: a `[[ ... ==` comparison split before its right operand. The shared
release check now keeps both operands on one line. Stabilization continues to
repair only the first exact protected-CI boundary with focused reproduction;
the complete regression remains owned by a fresh protected-main run.

## 2026-07-29 — Decision 140: Sealed entry points do not mutate their release

Category: Incident

Direct Linux execution of the deterministic state machine cached imported
Factory modules inside the sealed release, invalidating its authenticated tree
after a successful transition. The entry point now disables bytecode writes
before loading Factory modules. A sealed release must remain byte-for-byte
stable throughout planning, receipt consumption, execution, and recovery.

## 2026-07-29 — Decision 141: Budget accounting is independent of evidence reuse

Category: Incident

The Contract 1.8 budget reducer previously invoked complete passport evidence
validation, so an orphaned or otherwise non-reusable successful role output
could prevent spend calculation. Terminal accounting identities and charges
are now reduced without reading role output. Passport export and lifecycle
reuse still perform strict output validation, while every terminal charge
continues to count toward the business budget.

## 2026-07-29 — Decision 142: Cursor isolation names its lane and global roots

Category: Incident

Provider concurrency generalized attempt roots for activated products, but the
development-lane invariant continued to require its short Cursor scratch path
to be visibly anchored at `DEVELOPMENT_LANE_ROOT/c`. Runtime preparation now
branches explicitly: disposable lanes use that path, while installed global
coordination uses its authenticated owner-local runtime root. Both retain
private home, configuration, data, cache, output, and temporary directories.

## 2026-07-29 — Decision 143: Development lanes share the configuration lock

Category: Incident

Protected Linux release CI proved that the disposable development lane enabled
digest-bound concurrent provider admission without exposing the owner-local
configuration lock required by the coordinator. Four synthetic tickets
therefore failed at their first role reservation even though the production
launcher already supplied the lock. Every development lane now creates a
mode-0600 configuration lock inside its authenticated runtime root and passes
that exact path to lane-contained provider work. Admission and configuration
changes remain serialized without weakening policy-digest validation.

## 2026-07-29 — Decision 144: Certification consumes concurrency JSON on stdin

Category: Incident

The first live Contract 1.8 certification at capacity four proved that its
provider check returned valid ready JSON, but the evidence normalizer launched
Python with a here-document that replaced the check-output pipe. The parser
therefore read EOF and certification failed before product tests. The
normalizer now runs as an inline Python command whose standard input remains
the provider check output. It still validates `status=ready` and binds the
exact Factory SHA and tree before any receipt can be issued.

## 2026-07-30 — Decision 145: Terminal export follows authenticated migration lineage

Category: Incident

Nysa T-093 finished Test-author before two authorized release migrations, but
each successor controller migrated its passport before exporting the terminal
result. New versioned migration edges now retain the exact authenticated source
passport file digest, so a receipt is cryptographically bound to the complete
suffix. T-093's pre-v2 snapshot requires one exact protected-main bridge whose
commit, path, blob, receipt, old/current passport digests, complete legacy
history, target identity, and terminal accounting are revalidated at export.
Terminal Factory and contract must also equal the receipt. This preserves the
successful role without replay while arbitrary ancestry and broad recovery
bypasses remain rejected.

## 2026-07-30 — Decision 146: Dependency waits preserve exact repair ownership

Category: Incident

Nysa T-094 had an authenticated Test-author repair owner, but its later
dependency-wait receipt hid the original contract-block receipt and a Factory
upgrade made the signed repair record appear stale. The state machine now
accepts that immutable record only through one exact authenticated v2 passport
migration suffix and the unique unsuccessful blocker charge. This resumes only
Test-author without replaying Planner, Spec-linter, or Builder; broken or
ambiguous lineage still refuses.

## 2026-07-30 — Decision 147: Fulfillment and cutover controls may be atomic

Category: Incident

The protected T-040/T-092 dependency-fulfillment batch was introduced in the
same manual control commit as its required in-flight release authorization, but
the runtime validator expected only the pin and fulfillment files. It now
accepts that one exact additional path only when its filename names the same
target Factory SHA. Other Factory, application, test, contract, and CI paths
remain outside the atomic allowlist.

## 2026-07-30 — Decision 148: The launcher is part of the activation tuple

Category: Incident

Nysa generation 32 activated the sealed `4d726fb...` release while the stable
installed launcher still had an older Contract 1.8 command parser. The release
correctly issued a dependency-refresh receipt, but the old trust root rejected
that action before consumption. No provider ran or charged. Certification and
every receipt validation now require the installed launcher to be
byte-identical to the sealed candidate. A changed launcher is explicitly,
atomically bootstrapped while work is drained and the prior executable remains
the rollback artifact; activation never discovers protocol drift through a
live ticket again.

## 2026-07-30 — Decision 149: Cursor readiness never uses the source home

Category: Incident

Nysa generation 35 proved that Cursor's task-free readiness commands rewrite
`cli-config.json`. Invoking them with the operator's source home changed an
owner-only file to mode `0644`, after which the strict role-time credential
copy correctly refused every Cursor role before GO. Cursor route readiness now
validates and copies both session files into one disposable owner-only home,
runs version, contract, authentication, and model checks only there, and
removes it afterward. Present but unsafe or partial source state fails before
Cursor is invoked; role-time credential checks remain unchanged.

## 2026-07-30 — Decision 150: Real waits settle one controller invocation

Category: Incident

Generation 36 showed that a waiting ticket could be resubmitted after a short
cooldown while a sibling worker kept the same one-shot alive. Repeated
dependency and admission checks then reacquired and released leases without
provider work. A ticket that returns waiting, blocked, budget, error,
maintenance, or active is now settled for that invocation. External terminal
evidence or the next launchd invocation is the wake boundary; live siblings
and newly admitted tickets may still proceed concurrently.

## 2026-07-30 — Decision 151: The repair record, not the latest receipt slot, is the checkpoint

Category: Incident

T-094 retained a valid authenticated Test-author repair after multiple
dependency and release migrations, but a later transition had replaced the
original consumed Builder receipt file. Controller recovery now validates the
current remote passport and delegates the exact current repair stage to the
deterministic state machine. Only a valid non-refusal reopens the claim, and
the resolver-issued receipt is reused unchanged. Invalid repair or lineage
evidence remains blocked and a newly acquired lease is released.

## 2026-07-30 — Decision 152: A successful repair consumes its active checkpoint

Category: Incident

T-094 proved that recovering the correct repair role is insufficient if its
signed repair record remains active after the role succeeds. Normal v2
migration history now survives terminal passport export, while a history that
contains a one-use lineage authorization remains intentionally consumed.
After exactly one successful owner-role terminal, the state machine archives
the signed repair record. A legacy export that already consumed its migration
history may reach that boundary only through the exact consumed `FIX` receipt,
parent passport-file digest, authenticated completed-role evidence, immutable
manifest, charge, head, Factory release history, and original blocker charge.
If that export is migrated before the next transition, the same completion may
be recognized only through a contiguous v2 suffix that starts at the repair
Factory and a descendant of its head and ends at the current Factory, head,
protected base, route plan, and passport parent. A legacy authorization,
broken chain, missing, duplicate, or ambiguous proof may not retire a
checkpoint.

## 2026-07-30 — Decision 153: Protected test conflicts return only to Test-author

Category: Incident

Nysa T-094 exposed the remaining dependency-refresh recovery gap: protected
main and the ticket both modified one regular protected test, so the safe merge
refused and the controller parked the ticket even though ownership was
mechanically unambiguous. Dependency refresh now records the exact base,
ticket, and protected blobs, retains the protected test as the merge baseline,
and creates one authenticated Test-author repair checkpoint. Earlier roles and
charges remain valid. Sibling publication does not invalidate the signed
historical repair, and a merged ticket closes before dependency refresh.
Checkpoint retirement requires exact receipt/head/passport/evidence/charge
bindings plus an allowlisted regular-file diff. Application, mixed-owner,
control, contract, CI, configuration, rename, add/delete, non-regular,
missing-receipt, unknown, or tampered conflicts continue to refuse.

## 2026-07-30 — Decision 154: Opaque successful repairs retain conservative accounting

Category: Incident

T-094 completed its exact protected-test repair through Cursor CLI, but that
transport cannot report actual usage and therefore charged the full
reservation as `abandoned_conservative`. The dependency-conflict validator
alone rejected that valid terminal even though ordinary passport and
publication paths already accept it. Exact-stage repair now recognizes either
completed accounting or a conservative charge only when the immutable
manifest proves `cost_basis=conservative_reservation` and equal effective and
reserved cost, and the passport charge matches the same manifest and state.
Cancellation, launch-void, missing or unequal cost proof, and unknown states
remain fail closed.

## 2026-07-30 — Decision 155: Completed repair migration starts at the terminal export

Category: Incident

T-094 completed its protected-test repair before the next Factory cutover.
The signed checkpoint correctly retained the pre-role head, while the
authenticated migration suffix correctly began at the later terminal export
head. Recovery now bridges those boundaries only through the exact consumed
FIX receipt, unique successful manifest and charge, authenticated role
evidence, allowlisted repair diff, and one contiguous v2 suffix ending at the
current Factory, head, protected base, route plan, and passport parent.
Test-author output is validated only through the terminal export, so a later
route journal is not misclassified as role output. A reviewed protected-control
base need not already be ticket ancestry solely to archive that completed
checkpoint; ordinary provider-free dependency refresh remains the immediate
next transition. Pending, ambiguous, broken, legacy-authorized, or
unknown-path histories remain fail closed.

## 2026-07-30 — Decision 156: Release recovery precedes waiting-ticket reconciliation

Category: Incident

T-100 proved that an authorized route cutover can leave a waiting claim with a
prior-release passport even though it is not `blocked`. Upgrade recovery now
normalizes blocked, claimed, and waiting claims before stage scheduling, but
acts only on a signed prior-release passport or durable pending migration.
Existing role evidence, charges, dependency waits, and specialist blockers
remain unchanged.

## 2026-07-30 — Decision 157: A stage is resolved once across maintenance

Category: Incident

T-094 was clean and provider-free when maintenance arrived between two
`next-stage` calls, turning a safe pause into a permanently blocked claim.
The state machine now retains its first deterministic resolution. The
controller rechecks maintenance before PR or provider work, leaves that receipt
unconsumed, parks the checkpoint, and releases its lease. Every other
state-machine or execution failure remains fail closed.

## 2026-07-30 — Decision 158: A materialized blocker survives lease rotation

Category: Incident

T-094 proved that a one-shot controller can restart after a contract blocker
is already authenticated, charged, committed, and exported but before the
operator repair begins. Its exact ticket lease may be replaced, while the
historical receipt intentionally retains only the old lease digest. Initial
block materialization still requires that historical lease. A later
idempotent block validation may use the fresh lease only when the authenticated
passport binds the same project, ticket, branch, Factory, contract, receipt,
unique blocker charge, role stage, blocked state, exact resume target, and
receipt-to-passport-to-current-head ancestry, with no successful evidence for
the blocked receipt. Missing, tampered, ambiguous, unblocked, or unrelated
evidence remains invalid.

## 2026-07-30 — Decision 159: In-flight cutover preserves blocked tickets

Category: Incident

A Factory defect may require a successor while a ticket is correctly
`Blocked-Escalated`. Protected in-flight authorization and passport migration
now accept that exact state alongside the existing nonterminal set, provided
the repository, protected authorization, source/target Factory, ticket,
branch, remote head, route journal, and state all match. Cutover preserves the
blocked state and evidence; it does not resume the ticket. Backlog, Canceled,
Done, unknown, partial, extra, and state-drifted entries remain invalid, and
the ordinary maintenance, active-run, and lease drain barriers still apply.

## 2026-07-30 — Decision 160: Repair directives are scoped by passport lineage

Category: Incident

A ticket may legitimately return to the same repair owner more than once.
The active directive is the unique normal commit whose parent belongs to the
current authenticated passport or its v2 migration history and whose commit
remains in current branch ancestry. Older same-role directives outside that
repair window remain immutable history and do not block recovery. Zero or
multiple in-window commits, merge commits, malformed additions, multi-path
changes, or unrelated head drift remain invalid.

## 2026-07-30 — Decision 161: Repeated blockers hand off through one authenticated lifecycle

Category: Incident

A later contract blocker may belong to an earlier role than the ticket's
visible coarse state. The operator replaces the one active repair-owner
directive in an exact ticket-only commit bound to the authenticated passport
window. The coarse state does not move backward; the signed repair record
authorizes only the earlier owner, and deterministic stages catch up after its
success. A completed signed repair authenticates the still-visible directive
as historical. Missing, mismatched, tampered, multi-directive, multi-path,
merge, or unrelated histories remain invalid. Every repair in this family
must be tested as a full sequence through the first normal downstream stage,
not only as isolated transition boundaries.

## 2026-07-30 — Decision 162: Every resume decision is bound to one blocked receipt

Category: Incident

A role-only directive and a prior Linear move must never authorize a later
contract blocker. Every resume requires one exact ticket-only commit containing
the chosen role and the current consumed blocked transition-receipt digest.
The pair is replaced for every later blocker, even when the role is unchanged.
Missing, stale, partial, duplicate, mismatched, multi-path, merge, or unrelated
decisions fail closed before a provider call. A completed repair remains
historical only through its signed matching role-and-receipt archive.

## 2026-07-30 — Decision 163: Preflight consumes the verified repair stage

Category: Incident

T-094 proved that deterministic repair ownership can precede the ticket's
visible coarse state: the state machine authorized `FIX planner` while the
ticket correctly remained Building, but preflight independently required
Planning and blocked before submission. The installed launcher now passes the
stage returned by exact receipt verification into its empty helper
environment. Preflight accepts a later coarse state only for the exact
verified `FIX planner` stage; normal Planner work still requires Planning.

## 2026-07-30 — Decision 164: Repeated sealed-release checks use immutable fixtures

Category: Incident

The first protected Linux run of the Planner-repair regression showed that its
copied release was writable even though the test treated it as sealed. A first
preflight could therefore add platform-specific Python bytecode and make the
second provenance check fail correctly. Repeated release invocations now use a
read-only fixture matching installation; the separate forged-tree, physical
drift, partial provenance, and Git-metadata refusal cases remain unchanged.

## 2026-07-31 — Decision 165: Migrated repair lineage ends at the role input

Category: Incident

A successful repair role advances the ticket from its authenticated input head
to a new output head; immutable migration history must not be rewritten to
pretend that the migration itself created the role output. For an active repair
that originated under an earlier Factory, its unique contiguous migration
suffix therefore ends at the consumed FIX receipt's role-input head. The
current authenticated passport may bind a descendant output head only when one
terminal manifest, one completed-role record, and one accounting record match
the exact receipt, role, run, Factory, input head, and output digest, while the
terminal passport names the receipt-bound input-passport file, the charge has a
canonical nonnegative micro-USD amount, and the original blocked charge remains
unique and unsuccessful. Any missing, ambiguous, unrelated, or duplicate
evidence fails closed. A valid successful role is never replayed merely to make
migration history end at its output. If release activation migrates that
terminal success passport before repair retirement, the original migration
segment still ends at the role-input head and one separately contiguous v2
segment begins at the authenticated descendant output. The two segments must
share the same boundary Factory, protected base, and route; the second must end
at the exact current Factory/head/base/route and bind the current passport's
parent digests. Disconnected or ambiguous segments fail closed.

## 2026-07-31 — Decision 166: A resumed repair consumes its retained blocked claim

Category: Incident

T-094 proved that an authenticated state-machine resume can create the exact
owner-only repair while the controller claim still retains the failed role and
receipt. Recorded-repair recovery now accepts that topology only when both
claim fields exactly match the signed record's blocker identity; the state
machine validates the complete repair and resolves its safe stage before the
controller clears them. Empty-field recovery remains supported, while partial,
mismatched, active, unauthenticated, or refusing repairs stay blocked.

## 2026-07-31 — Decision 167: Unconfirmed submission retries only across a release

Category: Incident

T-094 proved that a process-group wrapper can cross durable GO yet fail before
publishing its adapter-submission marker. The exact exit-125, zero-output,
zero-progress terminal keeps its full conservative charge and stays blocked
under the same Factory. A successor may reopen only after the signed remote
passport proves that charge exactly once; any repeated successor failure stays
blocked. Collision-resistant temporary marker creation narrows the original
failure surface, and new manifests record
`adapter_submission_unconfirmed` with their bounded diagnostic-output digest;
only the original empty-reason shape additionally requires empty output. The
sealed Contract 1.8 qualification
environment—not the local-only development scheduler—is the production-parity
pre-promotion proof for this recovery.

## 2026-07-31 — Decision 168: Ordered three-ticket qualification precedes promotion

Category: System change

The sealed Contract 1.8 environment, not a launcher smoke or the development
scheduler, must complete the explicitly authorized T-094, T-100, and T-093
qualification cohort before final Factory CI, installation, product
certification, or activation. The controller already accepted a three-ticket
target, but dispatch still required four independent tickets and the reducer
still required a four-ticket restart plus concurrent PR creation. Contract 1.8
now validates an acyclic three-ticket graph at product capacity three, requires
protected terminal truth for dependencies outside that graph, reduces the
exact three-ticket restart/relocation/publication lifecycle, and reserves the
PR-concurrency assertion for a four-ticket qualification. The sealed takeover
uses the canonical Nysa passports and provider ledger under the shared lock,
so qualification completes the real preserved lifecycles instead of fresh
copies. Full Factory CI, installation, Nysa certification, promotion, and
activation remain downstream of the green reducer. T-096 remains outside the
cohort and dormant.

## 2026-07-31 — Decision 169: Untrusted role history is quarantined before publication

Category: Incident

T-094's sealed Builder completed its application change but replaced the
authenticated role-input ancestry. The non-force push correctly refused, yet
the old runner left the cell on the unrelated output head and could not export
the failed charge into the input-bound passport. Non-Test-author mutating roles
must now retain their input as an ancestor. A clean rewrite is preserved under
an exact run-specific diagnostic ref, while the trusted wrapper restores the
branch, index, and worktree to the unchanged input and verifies that the remote
never moved. The typed terminal keeps its full conservative charge and cannot
replay under the same Factory. Only a successor may reopen it, and only after
the signed remote passport proves the input head, unique failed charge, receipt,
role, and absence of completed-role evidence. The protected Test-author rewrite
authorization remains unchanged and no path force-pushes.

## 2026-07-31 — Decision 170: A successor lease retires the prior release marker

Category: Incident

The first `bb05660` reconciliation migrated all three qualification passports
without a provider call, then T-094 failed ordinary scheduling because upgrade
recovery stored a fresh dispatcher lease beside the old lease's
`lease_released=true` cache flag. `ensure_lease` therefore skipped renewal and
attempted a duplicate claim, which correctly refused. Successful successor
renewal or reacquisition now removes that stale marker in the same claim write.
The exact recovered lease is reused; duplicate leases are still forbidden, and
failure still releases and parks the ticket.

## 2026-07-31 — Decision 171: Release migration preserves every terminal receipt

Category: Incident

The `bb05660` upgrade migrated T-094's passport, then cleared its failed Builder
receipt before typed recovery ran. Upgrade recovery had preserved only contract
blockers and successful terminals; every other terminal was treated like stale
claim cache. A release migration now retains any receipt with a unique terminal
manifest and leaves it blocked for the next exact recovery pass. Success goes to
terminal export, recognized failure goes to its typed recovery, and unknown
failure stays blocked. Only a receipt with no terminal evidence may be cleared.

## 2026-07-31 — Decision 174: Qualification restart proof is candidate-scoped

Category: Incident

Canonical takeover state can retain restart markers from earlier qualification
candidates. A marker's existence cannot prove that the currently sealed
Factory crossed its mandatory controller restart. Restart-boundary and
recovered markers are therefore keyed by the exact candidate SHA and must
contain exactly that Factory SHA, the controller-event schema, and the sorted
authorized qualification tickets. Legacy markers are ignored, while a
malformed marker for the current candidate fails closed.

## 2026-07-31 — Decision 175: Failed recovery clears only passported charges

Category: Incident

A valid remote passport authenticates ticket head and lineage but does not by
itself prove that the failed attempt being recovered is charged. Before
clearing a legacy push-failure or pre-submission-interruption receipt, the
controller requires its exact terminal export in the current passport. If it
is absent, the existing receipt-bound passport export must add it and the
controller must revalidate it. An authorized head mismatch migrates first and
then retries that same export. Claim clearance, remote validation, and lease
acquisition remain separate gates.

## 2026-07-31 — Decision 176: Successor budgets are frozen-candidate scoped

Category: Incident

Production-successor qualification retains every historical charge in the
authenticated passport, but its runtime and final qualification allowance
count only charges whose Factory SHA equals the frozen candidate. Earlier
candidate spend cannot exhaust the new candidate's cap. The runtime helper
must authenticate the exact successor manifest and launcher Factory SHA before
using that basis. A budget wait may reopen only across such a successor release
migration; same-release and ordinary production budget waits remain closed.

## 2026-07-31 — Decision 177: Tests-first ownership is frozen-contract scoped

Category: Incident

A higher numbered append-only frozen contract legitimately reopens
Test-author ownership without invalidating successful Planner or earlier role
evidence. Test immutability therefore resets only when one commit changes one
canonical ticket file, adds exactly one higher numbered `Frozen contract`
heading, and adds its matching `Freeze result — PASS`. Prose, incomplete or
removed markers, same/older versions, and mixed commits do not reset the gate.
Within each epoch, every test commit still precedes implementation. Never use
the reorder helper across merge-rich or authenticated role-input history; an
identical final tree does not preserve the exact role heads or sequencing.

## 2026-07-31 — Decision 178: A receipt withdrawal is not an authorization

Category: Incident

Operator-resume selection may search history for the exact current receipt,
but an authenticated commit that removes that receipt is not an authorization
candidate. A candidate's resulting ticket must contain exactly the one visible
repair role and current blocked receipt. This permits a receipt to be rebound
after its parent becomes passport-authenticated without treating the interim
withdrawal as a duplicate. Two actual in-window additions still remain
ambiguous and fail closed before provider GO.

## 2026-07-31 — Decision 179: Qualification does not require preliminary activation

Category: Incident

Protected product policy can advance while the installed production product
correctly remains on its last activated tree. A takeover now authenticates that
clean source tree against the activation and requires current protected main to
contain its commit; the qualification control worktree remains based on current
protected main with the existing narrow diff allowlist. This removes a hidden
certify-before-qualification loop without permitting divergent history or
changes outside the sealed qualification tree to reach production.

## 2026-07-31 — Decision 180: Qualification roles keep local verification scoped

Category: Incident

T-094's Builder remained productive but spent the full ninety-minute role
boundary running the product's root workspace test command. Builder now runs
only the narrowest existing ticket-scoped tests and targeted lint or typecheck;
protected CI and final certification own broad verification. When a first
qualification Cursor attempt fails after leaving permitted partial changes,
the trusted fallback snapshots those changes before passport export, then
migrates the failed charge onto the resulting clean exact head. This keeps the
partial implementation and accounting without requiring manual cleanup or
replaying earlier roles. Its Builder boundary accepts only the current ticket's
required root-cause log as an exception to the Factory-control ban; sibling
tickets, tests, route journals, and other Factory controls remain forbidden.
The first-attempt limit counts submitted GO attempts only for the failed run's
exact frozen candidate; preserved attempts from predecessor candidates remain
accounted but do not disable the successor's fallback.
A sealed successor may checkpoint the latest exact failed candidate before
route migration: the failure remains bound to its old journal SHA, while the
local successor manifest must bind the executing sealed release. The resulting
clean handoff head is then eligible for ordinary route and passport migration.
Recovery remains valid after that migration only when the journal suffix is
entirely release migrations and one unique ancestor commit binds the exact
fallback revision trailer. The upgraded claim becomes runnable with its receipt
intact so terminal accounting finishes before the role resumes.

## 2026-08-01 — Decision 181: Publication evidence follows the canonical ticket ledger

Category: Incident

Qualification intentionally separates sealed controller manifests from the
canonical linked ticket worktrees. Role execution reduces effective accounting
beside the ticket repository's main checkout, so later publication helpers
must pair sealed manifests with that same canonical runtime ledger. Ticket PR
preparation and ticket attestation now resolve the ledger from the claimed
worktree (unless the launcher supplies an explicit trusted override). They do
not fall back to the qualification control checkout's stale ignored ledger.

## 2026-08-01 — Decision 182: Qualification isolates mutable accounting, not logic

Category: Incident

The canonical ignored runtime ledger cannot safely serve two controllers that
reduce different run-manifest roots: production Linear sync can replace the
qualification view and erase a just-completed Reviewer row. The sealed
qualification launcher now binds both runtime and durable ledger paths inside
its isolated product worktree, and every helper receives that trusted override.
Stage selection refreshes the lane-local projection from the lane's own
manifest root before consuming it. Production continues using its canonical
path. Both lanes execute the same release code and reducers; only mutable
runtime evidence is separated.

Narrator also receives the exact PR, head, validated Railway preview endpoints,
green required-check result, and current accounting snapshot from trusted host
boundaries. It must not rerun repository verification. An explicit
`NOT APPROVABLE:` bundle is never attested and consumes the one bounded
Narrator retry before escalation.

## 2026-08-01 — Decision 183: Qualification provider admission is candidate scoped

Category: Incident

The state-machine budget reducer already counts only the frozen successor
candidate, but the shared provider coordinator previously identified every
takeover checkout by the generic basename `product_`. Same-day predecessor
attempts could therefore exhaust a new candidate before provider GO. The sealed
launcher now binds provider product and ticket admission to project plus exact
candidate SHA. The machine-day scope remains shared and unchanged.

## 2026-08-01 — Decision 184: Successor launch-void recovery is schedulable

Category: Incident

A sealed successor can authenticate a predecessor candidate's typed pre-GO
`launch_void` without replaying or charging it, but recovery must classify the
preserved receipt as runnable before selecting the qualification restart
cohort. The ordinary terminal reducer then clears that receipt exactly once
and resumes the same stage. Recovery requires the exact abandoned/no-GO/
no-submission/zero-cost/launch-void tuple. Same-release and malformed receipts
remain blocked, so this bridge cannot create a retry loop.

## 2026-08-01 — Decision 185: Takeover provider scope binds the sealed lane root

Category: Incident

Production-successor takeover intentionally executes against a linked product
worktree outside the sealed qualification root. Candidate-scoped provider
identity validation therefore authenticates the launcher-supplied sealed lane
root and exact project/Kit-SHA tuple; it does not require the product path to be
nested under that root. If a role subprocess exits without a receipt-bound
terminal manifest, the controller blocks with the receipt intact and releases
the lease instead of resolving the same stage again.

## 2026-08-01 — Decision 186: Narrator retry exhaustion is a typed terminal escalation

Category: Incident

The sequencer's second structurally invalid evidence bundle is an expected
`ESCALATE` transition, not an unknown resolver result and not another role
retry. The typed state resolver accepts that non-role action, and the controller
blocks the claim once, releases its ticket lease, and records the escalation
detail. An explicit `NOT APPROVABLE:` bundle represents a product or deployment
failure and routes directly to Builder without another Narrator attempt. Both
paths preserve the exact failed bundle and screenshots for repair.

Every newly proven Factory edge case must add the smallest deterministic
regression that reproduces its original boundary before the repair is accepted.
The regression remains in the focused suite so incident evidence accumulates
into durable coverage instead of being discarded after the immediate fix.

## 2026-08-01 — Decision 187: State-machine helpers own their time bounds

Category: Incident

State-machine reconciliation may compose several independently bounded
resolver, ticket-state, passport, and Git operations. The controller must not
wrap that composition in a shorter aggregate timeout: a nested ticket-state
operation can commit and push before the parent returns, making an outer kill
look like failure after durable success. Replay authenticates the resulting
branch and resumes from its exact materialized state; it must not repeat the
transition or discard prior role evidence.

## 2026-08-01 — Decision 188: Exhaust the cheap role/state matrix first

Category: Process

Before resuming an expensive sealed provider role after a Factory repair, run
the deterministic role/state matrix. It covers every `RUN` and `FIX` role from
Ready, Planning, Building, and Review, including exact multi-hop transitions,
the Review-to-Building repair edge, no-op same-state replay, and forbidden
backward transitions. Role work is mocked; the real typed state-machine logic
remains under test. Known interruption, restart, accounting, dependency, and
publication faults remain covered by the focused controller composite replay.

## 2026-08-01 — Decision 189: Validate transition envelopes before side effects

Category: Trust boundary

The controller must authenticate the semantic shape of every state-machine
result before provider, GitHub, Railway, attestation, or publication work. The
schema, status, ticket, action, detail, receipt digest, typed stage, and exact
stage-to-role mapping must all agree. A malformed or mismatched envelope blocks
and releases the ticket lease; maintenance and ordinary reconciliation use the
same validation rule.

## 2026-08-01 — Decision 190: Linear transport retries are typed and bounded

Category: Reliability

The Linear synchronizer may retry `429`, `500`, `502`, `503`, and `504`
responses within one cycle, with no more than three total attempts. A valid
`Retry-After` is clamped to 0–30 seconds; a missing or malformed value uses
bounded exponential backoff. GraphQL semantic errors, malformed responses, and
other HTTP failures remain fail-closed and are retried only by the next normal
sync cycle. Repeated successful cycles must remain idempotent.

## 2026-08-01 — Decision 191: Narrator raster evidence is trusted only by bundle reference

Category: Trust boundary

A successful Narrator may commit its current ticket bundle and the screenshots
that bundle references after the latest Reviewer. Publication lineage treats
only exact current-ticket PNG paths as control evidence: additions and retained
images must be referenced by the current bundle, while deletions must have been
referenced by the reviewed bundle. Every admitted image is a bounded ordinary
Git blob with valid PNG boundaries. Unreferenced files, sibling-ticket paths,
symlinks, disguised content, and application paths remain implementation drift.
The successful Narrator output is preserved and attested at its exact branch
head; this boundary never authorizes a Reviewer or Narrator replay.

## 2026-08-01 — Decision 192: Publication validators share Narrator evidence classification

Category: Trust boundary

Ticket-PR readiness and bundle attestation must classify post-review Narrator
evidence through the same helper. The helper validates the complete PNG chunk
stream and CRCs, exact IHDR/IDAT/IEND structure, ordinary Git mode, current
ticket path and bundle reference, 2 MB per-file limit, and 32-file aggregate
limit. A new acceptance or refusal rule therefore changes both publication
boundaries together, while focused integration tests retain each boundary's
call-site behavior.

## 2026-08-01 — Decision 193: Approval continuation is shared immutable evidence

Category: Trust boundary

The Factory's approval attestation is a legitimate post-review continuation,
not application drift. Ticket-PR readiness and ticket attestation must validate
that continuation through one shared helper. Admission requires the exact
two-commit bundle/approval chain, ordinary `100644` receipt blobs, exact ticket
and receipt-only commit shapes, matching repository/branch/Kit-SHA/PR/bundle
identities, ordered timestamps, and the one permitted ticket transformation
from Awaiting Approval to Approved with `Operator-Approval: Linear`. Duplicate
JSON keys, altered receipt identity, approval-time ticket scope changes, extra
paths, executable evidence, or a non-direct commit refuse before publication.
If an already approved ticket crosses a sealed successor boundary, the helper
locates the unique approval-addition commit, validates it under its original
Kit-SHA, and permits only a later validated route migration plus the exact
ticket Kit-SHA replacement. The approval receipt, bundle receipt, bundle
document, and remaining approved ticket text stay byte-identical.
This rule preserves Reviewer and Narrator output while allowing the protected
H2 approval head to run checks and reach auto-merge.

## 2026-08-01 — Decision 194: GitHub truth closes projected approval publication

Category: State machine

Linear's operator overlay is transient authorization input, not proof that
GitHub accepted protected auto-merge. After the exact approval receipt exists,
phase two may retry from that immutable receipt when Linear has projected only
the state/approval fields away; a partial overlay refuses. A deterministic
`protected auto-merge requested` stage never lets the controller merely wait:
it reacquires the exact publication lease, invokes the idempotent approval
request, and verifies GitHub bound it to the exact H2 PR head. Post-merge
approval validation follows the same successor-continuation lineage, so a
sealed route migration between approval and merge remains valid without role
replay.

## 2026-08-01 — Decision 195: Post-merge propagation is a wait, not a ticket failure

Category: State machine

Protected-main closeout distinguishes missing, pending, and completed
unsuccessful required checks. Missing or pending contexts record a wait and
retry the same closeout claim; a completed unsuccessful context and ambiguous
duplicate evidence remain fail-closed errors. This prevents ordinary GitHub
check propagation after merge from parking an otherwise valid ticket without
weakening the Done boundary.

## 2026-08-01 — Decision 196: Merged passport truth survives publication-lease release

Category: State machine

A transient publication lease is not required to recover a merged ticket.
Before dependency tracking or ordinary state-machine evaluation, the controller
uses a safe current passport with `publication_state=merged` as authority to
verify exact GitHub merge truth and enter protected-main closeout. It releases a
publication lease only when one still exists. This preserves post-merge
recovery across a sealed successor without reopening prepublication stages.

## 2026-08-01 — Decision 197: Closeout wait terminates at exact Done attestation

Category: State machine

Merged-passport recovery returns a controller wait while its protected closeout
PR is open. Once it merges, `ticket-attest done` revalidates the exact closeout
commit, Done receipt, ledger, original protected merge and checks, and closeout
merge. Only that successful retry permits `ticket_complete` and ticket-lease
release; the controller must not loop or reopen branch-oriented prepublication
dependency logic.

## 2026-08-01 — Decision 198: Normal terminal evidence preserves successor routes

Category: Trust boundary

A normal bundle and approval remain bound to their original Kit-SHA and route
blob. When Done is recorded after sealed successors, its newer Kit-SHA is valid
only if protected main retains the exact attested route journal as a prefix and
adds a hash-linked suffix containing only release migrations that reaches the
Done and ticket Kit-SHA. Terminal and dependency readers validate that lineage
instead of requiring the current route blob to equal the historical bundle.

## 2026-08-01 — Decision 199: Product Done suppresses historical claim recovery

Category: State machine

A retained passport is audit evidence, not scheduling authority, after the
sealed product ticket has exactly one `State: Done`. Reconciliation renews and
releases any residual claim before scheduling and excludes the ticket from
passport recovery. The passport remains intact for audit and qualification
reduction, while malformed, missing, or non-Done ticket state stays eligible for
ordinary fail-closed recovery.

## 2026-08-01 — Decision 200: Qualification restart counts terminal targets

Category: State machine

A protected Done qualification target satisfies the cohort restart boundary
without retaining or reconstructing a runnable claim. Restart and recovery
events bind the complete configured ticket set, while only unfinished claims
are scheduled. This permits a sealed successor to continue the remaining
tickets without replaying terminal work or waiting forever for a third claim.

## 2026-08-01 — Decision 201: Completed Planner repair retains catch-up authority

Category: State machine

After a signed Planner repair is archived, its immediate resolved successor
may still target Planning beneath a Building or Review coarse state. The state
machine preserves that one authenticated override through the catch-up stage;
ordinary backward state materialization remains forbidden.

## 2026-08-01 — Decision 202: Generated fixtures must be producible

Category: State machine

The planning and checking roles evaluate exact generated fixture expectations
from their frozen initializer/reset. A value the setup cannot produce, or a
repair scope that forbids the required setup correction, blocks before Builder.

## 2026-08-01 — Decision 203: Repeated blocks replace resume baselines

Category: State machine

A remote/local Blocked-Escalated observation clears the prior state/approval
overlay before effective-state materialization and records a new timestamp
baseline. Sharing the same coarse state with an earlier block does not preserve
that earlier resume authority.

## 2026-08-01 — Decision 204: Backward repair blockers retain coarse state

Category: State machine

An active signed backward repair may enter Blocked-Escalated directly from its
unchanged later coarse state and records that state as `Resume-State`.
Idempotent block recovery and later resume authenticate the same exact repair;
without it, any role/state mismatch remains fail-closed.

## 2026-08-01 — Decision 205: Blocked repairs migrate through terminal evidence

Category: Trust boundary

When a repair owner contract-blocks before a successor migration, its active
repair may survive only through the exact consumed FIX receipt, parent blocker,
unique terminal manifest, authenticated charge, unchanged passport stage, Git
ancestry, and contiguous release suffix. The descendant migration edge by
itself is not authority.

## 2026-08-01 — Decision 206: Operator state overlays bind their ticket source

Category: State machine

A Linear state overlay is valid only for the exact committed ticket text from
which it was ingested. Any later ticket commit, or a legacy unbound overlay,
clears it before projection so a repeated block cannot be masked. Recovery
waits for the exact current blocker receipt to become visible before asking the
state machine to authenticate resume.

## 2026-08-02 — Decision 207: Freeze producers and epoch consumers share one grammar

Category: State machine

Planner emits one canonical append-only `Freeze result: PASS` marker for every
new numbered contract. The gate and reorder helper also accept the established
historical marker forms and an exact one-for-one replacement of only the latest
heading and its matching PASS with a higher version. Git preserves the prior
role input; partial removal, mismatched versions, multiple markers, lower
versions, mixed commits, and malformed supersession remain fail-closed.

## 2026-08-02 — Decision 208: Pending operator commits authenticate at the passport boundary

Category: Trust boundary

Idempotent block recovery may inspect the one exact receipt-bound operator
commit directly after the authenticated passport head. It validates the active
repair at that prior authenticated head and then migrates the passport through
the already-validated directive commit before resume. Any unrelated, ambiguous,
multi-path, merge, stale-receipt, or non-passport-parent descendant remains
fail-closed.

## 2026-08-02 — Decision 209: Pause intent is distinct from missing-claim recovery

Category: State machine

An operator pauses one idle ticket through a passport-, branch-, head-, exact
in-flight lifecycle-state-, and unique-worktree-bound owner-local intent. The
claim and lease are released but the passport and recorded status remain.
Only explicit resume can reconstruct that exact claim; lifecycle drift,
merged/Done truth, or full capacity leaves the intent untouched. Discovery and
interrupted-reconciliation recovery never turn an arbitrary or paused passport
into scheduling authority.

## 2026-08-02 — Decision 210: Pre-provider interruption recovery is marker-bound

Category: State machine

Before a deterministic worker starts, the controller records the exact current
passport, release, head, route validity, and ticket run-manifest snapshot. A
receipt-free untyped block may reopen once only if those inputs, the clean
remote worktree, and absence of runs, terminals, pauses, and typed blockers all
remain exact. Every mismatch stays blocked.

## 2026-08-02 — Decision 211: Linear authority clears survive stale board snapshots

Category: Trust boundary

Full-board network work owns a cycle lock, not the map lock. Consuming an
operator overlay durably records its exact version before a short locked map
write; every later board save applies that intent until successful
reconciliation proves it retired. Repeated identical unsafe admission inputs
share one durable incident and bounded reminders without weakening admission.

## 2026-08-02 — Decision 212: Late test repairs open a new tests-first epoch

Category: State machine

Contract 1.8 routes Reviewer-requested Test-author work through one Planner
repair first. That repair may change only the ticket and must append one higher
canonical frozen-contract/PASS pair. The sequencer validates the commit before
Test-author, while a Planner PATH guard prevents package-manager product suites.
Prior Reviewer and Narrator output remains immutable evidence.

## 2026-08-02 — Decision 213: Certification capabilities are plan inputs

Category: Trust boundary

Certification plan v2 pins Node and npm separately and makes every phase's
network requirement explicit. Required network without reviewed opt-in fails
before spawn; denied phases stay denied even when another phase is granted.
Receipts bind declared/granted capability and runtime, failures preserve
redacted hash-bound evidence, and an existing product's canonical activation
path/origin/generation is checked before expensive phases.

## 2026-08-02 — Decision 214: Successor recovery overlaps only across tickets

Category: Performance

Authenticated migration and resume for independent tickets may overlap up to
the certified ticket capacity. Within each ticket, passport migration, route
binding, lease replacement, repair authentication, and accounting remain in
their original order behind the same launcher controls.

## 2026-08-02 — Decision 215: Linear projection binds blocker generations and serializer semantics

Category: Trust boundary

Every committed blocked ticket source has its own digest-bound remote timestamp
baseline even when no operator state overlay exists. Description comparison
canonicalizes only Linear's observed ordered-list, inline-code, link, and fence
round trips; nested structure, fenced content, and meaningful edits remain
different and are restored from Git.

## 2026-08-02 — Decision 216: Frozen test scopes close fixture lifecycle dependencies

Category: State machine

Planner and Spec-linter trace every required serialized suite through setup,
reset, and teardown before contract freeze. Parent cleanup names every sibling
dependent table and includes child-first cleanup for each non-cascading foreign
key; an exact `ON DELETE CASCADE` needs no redundant edit. The frozen
Test-author scope contains only those required setup corrections. If it does
not, Test-author preserves valid committed tests and blocks before Builder.

## 2026-08-02 — Decision 217: Migration approval binds one readiness snapshot

Category: Performance

Ordinary migration preview no longer serializes the complete retained journal;
an explicit diagnostic flag remains available. Preview exposes the exact
readiness digest, and apply accepts only that digest after one fresh probe
round, preserving approval, journal, and drift refusal without a duplicate
round in the same command.

## 2026-08-02 — Decision 218: Protected CI uses bounded isolated suite groups

Category: Performance

Protected-main CI runs four stable suite groups per platform on distinct hosted
runners. The factory-script, Hermes-contract, and factory-kit
lifecycle suites remain whole and sequential as canonical smokes; a checked
mapping assigns every suite exactly once and defaults newly registered suites
to release group 4. Whole-shard local execution stays sequential after
development-lane benchmarks showed shared-runner concurrency causes timeout
contention instead of a wall-time reduction.

## 2026-08-02 — Decision 219: Certification runtime identity is one tuple

Category: Trust boundary

For products that adopt certification plan v2, readiness, sealed qualification,
certification, receipts, and qualification launch share one strict Factory
SHA/tree, product SHA/tree, Contract, Node, and npm tuple. The common parser
rejects missing, unknown, malformed, or mismatched input before expensive work;
products without a v2 plan retain the opaque certification path.

## 2026-08-02 — Decision 220: Phase evidence reuse is exact and restart-local (superseded by Decision 223)

Category: Performance

A protected certification plan may opt a phase into evidence reuse only for a
complete, nonempty declared-artifact set; application tests, policy, security,
and configuration checks retain `never`, as do undeclared side effects. Reuse
stays inside one disposable result root and binds the exact Factory, product,
plan, dependencies, command, Node/npm and runner identities, and network
capability; the retained log and artifacts are rehashed before a hit.
Interrupted, stale, malformed, tampered, undeclared-side-effect, and
fresh-workspace cases execute or fail closed, while the outer certification
and receipt authority remains unchanged.

## 2026-08-02 — Decision 221: Accepted late tests require protected normalization

Category: Trust boundary

A successor may recover an already accepted late Test-author push only after
first migrating the authenticated passport on the unchanged old head. A
canonical one-file protected authorization binds the predecessor evidence and
charge, successor Factory, exact old/new tree-identical histories, route, and
protected merge parents; the controller waits for an explicit exact-head
force-with-lease and performs the passport migration once.

## 2026-08-02 — Decision 222: Blocked normalization does not grant resume

Category: Trust boundary

An exact `Blocked-Escalated` ticket may use protected accepted-test history
normalization while retaining that state. Normalization changes only the
authenticated tree-identical lineage; the existing receipt-bound blocker
resume remains the sole authority that restores its recorded resume state.

## 2026-08-02 — Decision 223: Composite successors preserve independent provenance

Category: Release management

Several authorized Factory repairs may share one release only after each exact
issue commit is independently green in its isolated branch. One successor from
the protected base cherry-picks those commits, preserves issue/source/candidate
provenance, runs combined readiness, and receives the single authoritative
protected-main CI and sealed certification cycle; unsafe or overlapping changes
remain separate.

## 2026-08-02 — Decision 224: State-machine recovery has no generic bypass

Category: Trust boundary

The exact current-receipt-bound `OPERATOR RESUME` ticket commit is the existing
emergency state-machine recovery authority. Envelope and semantic-round
overrides do not change lifecycle state; any future exception requires exact
owner authorization bound to its target and a dedicated GitHub issue before it
is implemented or used.

## 2026-08-02 — Decision 225: Normalized blocker lineage is exact and unique

Category: Trust boundary

A consumed contract-block receipt may cross protected history normalization
only through one authenticated same-release migration edge whose old and new
Git trees are byte-identical, or whose only protected history-repair delta is
an append-only current ticket log. Route and protected-base bindings must match
the current passport. Missing rewrite authorization, semantic drift, or an
ambiguous edge remains blocked; the receipt-bound operator directive is still
the only resume authority.

## 2026-08-02 — Decision 226: Certification runtimes are owner-locally pinned

Category: Trust boundary

The Factory verifies a product plan's exact Node/npm/npx executables before
atomically linking them into the sealed launcher's existing `~/.factory/bin`
PATH priority. The system-wide Homebrew selection remains untouched; unsafe
paths, source mismatch, or installed tuple drift fail closed before readiness,
qualification, certification, activation, or launch can spend work.

## 2026-08-02 — Decision 227: Passport export honors normalized blocker lineage

Category: Trust boundary

Before the controller can recover a normalized contract blocker, passport
export accepts its historical receipt only through the same contiguous
authenticated v2 suffix with exactly one authorized byte-identical rewrite.
Ordinary exports retain raw ancestry, and missing authorization, tree drift,
broken chaining, or ambiguity fails before state-machine recovery.

## 2026-08-02 — Decision 228: Artifact reuse crosses disposable certification safely

Category: Performance

Factory certification may persist only a phase's complete, nonempty,
plan-declared artifact set. The owner-only HMAC store and key stay outside the
product sandbox; verified entries enter as read-only disposable input, new
candidates exit separately, and the Factory revalidates, authenticates, and
atomically publishes them under bounded TTL, entry-count, file-count, and size
limits. Exact Factory physical tree, product identity, raw plan, dependency
digests, runtime, runner, command, network, file type, mode, path, and content
bindings are mandatory. Raw logs and test/policy/security/configuration or
undeclared side effects remain nonpersistent, and full product certification,
receipts, and protected CI stay authoritative.

## 2026-08-03 — Decision 229: Normalized blocker proof carries its active repair

Category: Trust boundary

An active signed repair bound to the exact consumed contract blocker may reuse
the blocker verifier's authenticated normalized lineage after its charge,
terminal, role, stage, and failed-role evidence agree. Later contiguous
same-release edges are not separate repair starts; ordinary repair migrations
still require one unique direct start, and no generic recovery bypass exists.

## 2026-08-03 — Decision 230: Tracking projection accepts exact convergence

Category: Reliability

After a trusted push, the remote-tracking compare-and-swap accepts a concurrent
update only when it already equals the verified pushed SHA. Every third value
remains a hard refusal and is never overwritten.

## 2026-08-03 — Decision 231: One converged Builder false terminal is correctable

Category: Reliability

A successor may synthesize completed Builder evidence without replay only for
the exact issue-218 post-success terminal shape and only through the trusted
passport/controller path. The consumed receipt, unique terminal and charge,
owner-only output and final-success progress, clean cell, current Factory,
authenticated direct or uniquely migrated passport lineage, and remote head
must agree; the signed correction persists across later exports and repeated
recovery is a no-op. This does not create a generic override: any emergency
exception still requires target-bound owner authorization and a linked issue.

## 2026-08-03 — Decision 232: Expected absence is an explicit tracking CAS state

Category: Reliability

The shared remote-tracking update translates an empty observed old SHA into
Git's zero OID, so a legitimate first-time model pin may initialize an absent
tracking ref without weakening compare-and-swap. An absent ref after a
nonempty observation still fails and remains absent; exact convergence remains
idempotent, and every third or unreadable state remains a hard refusal.

## 2026-08-03 — Decision 233: Converged-success recovery binds real terminal lineage

Category: Reliability

The exact successor-only Builder correction accepts only `cursor-openai` and
`cursor-anthropic`, matching the route catalog and terminal manifests. If its
authenticated failed-run export already advanced the passport from receipt
input to descendant output, one unique all-v2 successor suffix may bridge that
output to the current release only when release order, head/base/route chain,
and final source-passport parent digests all agree. Generic receipt lineage is
not relaxed; legacy, non-Cursor, unknown, disconnected, or ambiguous evidence
remains blocked.

## 2026-08-03 — Decision 234: Provider login shells retain the certified runtime

Category: Reliability

The sealed launcher passes the active certified Node/npm versions only to role
runs. Each isolated provider home restores the trusted task PATH after macOS
login initialization and refuses the requested product command when either
runtime differs; the provider CLI executable remains outside that product
runtime check.

## 2026-08-03 — Decision 235: FIX blockers resume from receipt-bound stage

Category: Reliability

A rotated lease may revalidate a materialized `FIX <role>` blocker whose
coarse resume state is later than the role only when the consumed receipt and
authenticated blocked passport name the same exact FIX stage. The state
machine no longer circularly requires the signed repair record before the
resume operation that creates it; all existing receipt, charge, terminal,
passport, ancestry, directive, state, and current-lease checks remain in force.

## 2026-08-03 — Decision 236: Exact-ticket Linear pulls bypass broad-board latency

Category: Performance

An already mapped and initialized ticket may ingest its Linear operator fields
through one exact-issue read without scanning projects or the shared board.
The targeted pull merges only operator-owned entry fields, preserves newer
observations across stale concurrent full-cycle saves, and does not claim
full-board health. Unmapped, changed, incomplete, timed-out, or rate-limited
pulls leave the overlay unchanged.

## 2026-08-03 — Decision 237: Migrated FIX blockers retain their lineage validator

Category: Reliability

The receipt-bound same-release FIX shortcut hard-fails malformed evidence only
when receipt and passport share a Factory SHA. A source-to-candidate mismatch
falls through to the existing authenticated contract-repair migration proof,
which remains solely responsible for release history, passport suffix,
normalization, charge, terminal, directive, ancestry, and current ownership.

## 2026-08-03 — Decision 238: Qualification takeover completes claimless Done targets

Category: Reliability

An exact protected-main Done successor target remains terminal after its claim
has been released, but takeover is explicit rather than inferred. Through the
surviving clean ticket cell, the controller authenticates and migrates its
merged source passport without a claim, role, publication lease, or charge,
then seals source/candidate passport digests and protected Done/PR identity in
one candidate marker and typed adoption event. Completion is append-once, the
adopted ticket alone requires no candidate publication pair, and duplicate
adoption, completion, acquisition, or release evidence fails reduction.
Restart and recovery continue to schedule only unfinished targets.

## 2026-08-03 — Decision 239: Terminal adoption follows the source release suffix

Category: Reliability

A claimless Done passport need not be current on the production source itself.
Successor adoption accepts it only when the shared ordered lineage verifier
finds one complete suffix from the manifest source through its immediate
pre-candidate Factory. The authenticated passport is preserve-migrated from
that predecessor to the candidate, and typed evidence binds both releases.
Disconnected, reversed, ambiguous, or candidate-containing pre-lineage fails
before migration, marker creation, or candidate completion.

## 2026-08-03 — Decision 240: Fresh migrated repairs reuse blocker proof

Category: Reliability

A receipt-bound resume may create its first signed repair record after a
Factory migration only when the historical consumed FIX receipt, unique
terminal charge, ordered release history, exact authenticated current passport,
and Git lineage already prove the same blocker. The operator directive remains
exact-receipt and ticket-only authority; passport-digest drift, missing history,
and ordinary cross-release state changes remain closed.

## 2026-08-03 — Decision 241: Terminal adoption follows the signed head suffix

Category: Reliability

A protected Done receipt continues to bind the merged PR head even when later
signed route migrations advance the ticket passport. Successor adoption and
the qualification reducer now share one exact v2 suffix check from that
approved head to the current authenticated passport, including Factory, base,
route, and parent bindings. Equality remains valid; historical membership,
disconnected or ambiguous suffixes, and substituted parents remain refused.

## 2026-08-03 — Decision 242: Operator resume provenance includes role-only edits

Category: Reliability

Receipt-bound repair discovery follows commits that changed either operator
directive line. A ticket-only role replacement may preserve the exact receipt
only when its single parent is an authenticated passport head and every
existing ticket, ancestry, Linear, and uniqueness check still agrees.

## 2026-08-03 — Decision 243: Qualification retains exact passport artifacts

Category: Reliability

Takeover retains only the authenticated completed-role artifact closure named
by authorized passports and restores it as ignored mode-600 runtime evidence.
Preparation and final role dispatch both refuse missing, changed, unsafe, or
ambiguous manifests, outputs, and progress journals before provider spend.

## 2026-08-03 — Decision 244: Fallback boundaries are failed-attempt generations

Category: Reliability

Append-only historical fallbacks may survive release migration, but a later
different authenticated role/run/manifest may append one new fallback.
Exact replay stays idempotent; a parked qualification claim reacquires its
lease and resumes through the existing passport-migrating finish path.

## 2026-08-03 — Decision 245: Missing operator maps refuse cleanly

Category: Reliability

Direct approval attestation treats an absent Linear operator map as no exact
approval and returns the existing refusal instead of a traceback. It does not
invent or weaken operator evidence.

## 2026-08-03 — Decision 246: Emergency closeout is exact and receipt-bound

Category: Trust boundary

An already-merged ticket whose normal approval chain cannot be completed may
close only through a fresh owner request linked to an open GitHub issue and the
exact SHA-256 of a sealed read-only plan. The plan binds protected main, merged
PR, configured successful checks, active release, authenticated passport, and
idle blocked claim;
an absent passport is accepted only for protected operator-built work with no
claim or passport. Apply reuses ordinary ledger and protected closeout
mechanisms, creates a distinct immutable terminal receipt, and is restart-safe
without fabricating bundle or Linear approval evidence.

## 2026-08-03 — Decision 247: Factory-script CI uses isolated internal workers

Category: Performance

The canonical `factory-scripts` suite ID and four protected-CI groups remain
unchanged, but the monolithic shell regression now dispatches six fixed
internal workers. Every worker receives a private temporary root; cases that
share launch, cancellation, Git, accounting, or cleanup assumptions remain
sequential within one worker. The parent replays successful logs in stable
order, reports the exact failed subset and assertion, and terminates surviving
worker process groups on failure or interruption. Focused regressions cover
parallel start, fixture-root isolation, failure diagnostics, and leaked-child
cleanup.

## 2026-08-03 — Decision 248: Terminal Linear projection follows protected truth

Category: Trust boundary

Ordinary and emergency closeout may update only the ticket's exact initialized
Linear issue, and only after the closeout receipt validates on protected main.
The updater sends only the Done state, re-reads exact Done, honors the
launcher-bound operator map, and returns evidence for one idempotent controller
event before claim release. Missing mapping, transport failure, changed source,
or unconfirmed state leaves closeout retryable without partial map writes.

## 2026-08-03 — Decision 249: Kit provenance is lane-scoped

Category: Trust boundary

Protected production install and certification retain their existing
`origin/main` ancestry plus exact successful main-CI proof. Trusted launchers
now label helper runs as `production-certified` or `qualification-candidate`,
and either label requires a SHA/tree-sealed release; mutable checkouts remain
`development-local` and cannot claim a trusted scope. This preserves fast local
and qualification iteration without presenting unreachable candidates as
production provenance.

## 2026-08-03 — Decision 250: Rework laps are receipt-bound and capped

Category: Reliability

Planner/Spec-linter, Builder/Reviewer, and authenticated contract-repair loops
reuse canonical ticket verdicts or signed repair records as their attempt
counter. The transition receipt and one idempotent controller event expose the
loop, attempt, limit, and stopped stage; the third failed lap escalates before
another provider call. The Markdown state remains the Linear-compatible coarse
business state, while one shared action-aware whitelist validates every state
mutation, including Reviewer and qualification back-edges.

## 2026-08-03 — Decision 251: Parking preserves an issue-bound repro

Category: Reliability

The existing passport-bound pause is the Factory's Parked primitive; no second
business or Linear state is added. A new pause requires an exact Software
Factory issue URL and preserves the release, head, passport, run snapshot,
current state, Resume-State, and claim status while releasing the ticket lease.
Resume requires the operator to name the exact active Factory SHA, revalidates
the portable checkpoint, and archives the repro record before execution can
continue. Historical v1 pauses remain resumable only through the named-release
boundary.

## 2026-08-03 — Decision 252: Rejected unblocks are visible without widening authority

Category: Reliability

After the reconciler has observed the current Blocked-Escalated generation, a
newer Linear move to any state other than the exact Resume-State remains
unauthorized. It now records one digest-bound `last_rejected` sync-health event
and posts one deduplicated Linear comment naming the required column and Git
receipt directives. Exact-ticket and full-board pulls preserve the same
evidence across overlapping saves. The operator transition whitelist remains
unchanged; the already-tested absent-map attestation refusal remains the
current behavior, and the ignored machine-local map is not re-tracked.

## 2026-08-04 — Decision 253: Role admission uses the launcher-bound CLI root

Category: Reliability

Contract 1.8 role admission passes the launcher-validated
`FACTORY_CLI_RUNTIME_ROOT` to the provider concurrency checker. Qualification
therefore uses its short owner-local Cursor path consistently with Doctor;
missing, unsafe, or overlong roots still fail closed before provider spend.

## 2026-08-04 — Decision 254: Reviewer repair epochs retain bounded catch-up authority

Category: State machine

An authenticated Reviewer rejection followed by its repair Planner may admit
Planning-level roles beneath Building or Review until the reopened test-first
epoch reaches Builder. The grant derives only from the authenticated ordered
role sequence and the still-current rejection; it expires at Builder and does
not permit an ordinary backward state transition.

## 2026-08-04 — Decision 255: Qualification restart proves recovery, not runnability

Category: Qualification

A candidate-scoped restart boundary accounts for every exact preserved target
claim plus protected Done targets, while scheduling still counts only runnable
claims. Successor upgrades therefore preserve intentional preflight blocks
without inventing passports, leases, or work; missing target claims still keep
the barrier closed.

## 2026-08-04 — Decision 256: Qualification authority outlives disposable scratch

Category: Qualification

The sealed release and Cursor scratch may remain under `/private/tmp`, but an
isolated qualification's controller passports/events, provider accounting,
HMAC key, and paused worktrees live under the owner-only
`~/.factory/qualification/<project>` root. Restore is supported only from a
signed safe pause with the exact Factory/product/manifest/runtime, branch/head,
stage, passport, pause, and run-snapshot identities; missing or changed evidence
never gains authority from Git history.

## 2026-08-04 — Decision 257: Paid evidence waits for exact deployment identity

Category: Publication

Green protected checks and a Railway success comment are insufficient for
Narrator. Every linked preview service must report the exact reviewed
repository, branch, and commit through Railway deployment evidence. Stale,
pending, or unavailable identity waits without a role charge, resets its bound
when the head changes, and emits one typed block after the bounded interval.
Reviewer passport migration also precedes cell parking so its authenticated
publication head can wait without a false controller error.

## 2026-08-04 — Decision 258: Terminal prerequisites and replay are exact and early

Category: Reliability

Qualification preflight hydrates only committed immutable PR heads required by
legacy and protected terminal records, verifies their exact SHA and ancestry,
and changes no branch, ref, worktree, or ledger. Once both implementation and
closeout PRs are merged, the controller persists an exact passport-, PR-,
Factory-, and protected-main-bound Done request. A clean parked terminal
`controller-error` may replay only that request; all other blocked claims remain
ineligible, and terminal events are idempotent.

## 2026-08-04 — Decision 259: Detached qualification registration is valid

Category: Qualification

The immutable registered product checkout may be detached at its exact
protected-main SHA while ticket work remains on authenticated ticket branches.
The role runner distinguishes this valid state from a Git error and continues
to guard exact HEAD, status, and tracked content throughout provider execution.

## 2026-08-04 — Decision 260: Unsubmitted attempts are not model fallbacks

Category: Qualification

A durable-GO Cursor terminal enters same-release model fallback only after a
task was actually submitted. An unsubmitted conservative terminal first gains
an authenticated passport charge, remains blocked under the current release,
and can resume only through the existing successor-release proof.

## 2026-08-04 — Decision 261: Compact route evidence is one shared contract

Category: Reliability

The migration writer and every downstream attestor accept the same legacy and
compact release-migration schemas. Compact evidence inherits the active
resolution only when its canonical digest matches; tampering and logical route
changes remain refused.

## 2026-08-05 — Decision 262: A signed pause is an emergency closeout boundary

Category: Reliability

When a Factory defect is discovered at a clean pre-provider boundary, ticket
control can remove the idle claim while preserving a controller-signed pause.
Emergency closeout may use that pause only when its digest, ticket, branch,
state, head, passport, run snapshot, and blocking issue match the authenticated
passport and the exact owner request. The ordinary semantic-loop cap remains
unchanged; an unsigned, stale, mismatched, or passportless pause grants no
authority.

## 2026-08-05 — Decision 263: Passportless preflight retries stay provider-free

Category: Reliability

An exact qualification Planner-preflight block may be retried only before any
passport, run record, or active process exists and while its current route,
clean remote head, unconsumed receipt, and manifest membership still agree.
The controller reruns sealed preflight under a fresh exact-ticket lease and
reopens ordinary reconciliation only on pass; every failed or drifted retry
stays blocked without provider spend.

## 2026-08-05 — Decision 264: Budget stops provider work, not trusted reduction

Category: Reliability

Once a successful role is fully accounted, reaching the ticket cap may not
mask deterministic state validation or the receipt-bound attestation that role
already produced. The sequencer substitutes `AWAIT_BUDGET` only for a resolved
paid `RUN` or `FIX` action; it never weakens provider admission or evidence
validation.

## 2026-08-05 — Decision 265: Pause resume identity follows lifecycle state

Category: Reliability

A controller-signed pause records JSON null Resume-State outside
`Blocked-Escalated`, where no resume overlay exists. Emergency validation
accepts that exact null while continuing to require a nonempty resume state for
`Blocked-Escalated`; every other malformed value remains refused.

## 2026-08-05 — Decision 266: Clean qualification creates only ignored runtime

Category: Qualification

The trusted preparer creates an absent physical owner-only `factory/runs/`
after the clean-worktree check. It rejects unsafe replacements, noncanonical
Product-Decisions metadata, and selected dependency pairs before sealing; the
ledger remains fail-closed and no provider work starts on preparation failure.

## 2026-08-05 — Decision 267: Linear recovery is identity-bound and selected

Category: Operations

A lost ignored map may adopt only one same-team Project whose durable marker or
Factory-managed issue membership proves the initiative identity. Qualification
initializes only selected unmapped tickets after canonical setup; ambiguity,
same-name identity gaps, and bounded rate limits remain provider-free waits.

## 2026-08-05 — Decision 268: Static contract collisions fail before Builder

Category: Planning

Planner conflict declarations use the readiness parser's exact grammar. New
global-shell literals are checked against bounded static protected-test text
assertions before freeze; a collision names its test and assertion and requires
explicit Test-author ownership rather than weakening the protected check.

## 2026-08-05 — Decision 269: Preflight refusal evidence precedes lease release

Category: Reliability

The controller persists only bounded redacted failure lines, exit/reason codes,
and the full-output digest before a Planner preflight block releases its lease.
Malformed or oversized refusal evidence gets a distinct closed block, so later
lease state cannot mask the original deterministic cause.

## 2026-08-05 — Decision 270: PR waits can wake inside a live reconcile

Category: Throughput

A PR-gated waiting claim is reconsidered on the existing reconciliation
interval while another worker remains live. The scheduler neither busy-polls
nor launches twice; generic waits remain settled and stale, pending, or failed
head evidence remains closed.

## 2026-08-05 — Decision 271: Evidence never silently crosses Factory identity

Category: Reliability

Route migration fails before preview when an existing bundle binds a different
Kit-SHA. Conversely, validated protected-main Done is authoritative for
qualification admission even when the sealed registered checkout intentionally
retains a nonterminal ticket; terminal work is never reclaimed.

## 2026-08-05 — Decision 272: CI setup avoids temporary runtime compatibility

Category: CI

Factory and generated workflow pins use official Node-24 action-runtime majors.
The macOS GNU-timeout install disables Homebrew auto-update so unrelated taps
are neither trusted nor mutated; protected CI remains authoritative.

## 2026-08-05 — Decision 273: Planner catch-up is derived, not overridden

Category: State machine

After a Reviewer rejection, only the authenticated alternating
Planner/Spec-linter repair prefix may preserve a Planning-level role beneath a
Building or Review coarse state. Receipt verification derives one
`CATCHUP planner` preflight admission only for an uncapped
`planner-spec-linter` attempt below the three-failure ceiling. The existing
issue-bound pause/successor restore, receipt-bound operator resume, and
hash-approved emergency closeout remain the manual recovery paths; no generic
state-machine bypass is introduced.

## 2026-08-05 — Decision 274: History repair separates authorization and replay bases

Category: Reliability

A failed Test-author push may publish one protected, issue-bound, expiring
mixed-history repair only when the signed passport separately identifies the
current protected authorization parent and its earlier ticket replay base.
The old/new histories must preserve every per-path non-Factory patch and
protected merge while the final tree changes only by the authenticated
append-only ticket log. Migration retains the failed charge exactly once,
never records false success, and resumes through the ordinary state machine.

## 2026-08-05 — Decision 277: Successor history repair preserves failed-release identity

Category: Reliability

A successor validates a failed Test-author repair against two Factory
identities: the current recovery release and the historical release recorded
by the consumed receipt and terminal manifest. The latter must already occur
in the authenticated passport release history. Migration must not relabel old
evidence with the successor SHA.

## 2026-08-05 — Decision 275: Emergency admission consumes only an unchanged role receipt

Category: Safety

Contract 1.8 may fall back from ordinary receipt consumption to one open-issue,
hash-approved, expiring authorization for the exact ticket, role, receipt,
head/tree, route, authenticated passport, lifecycle state, Factory tree, and
current lease. Apply is inert until the ordinary launcher rejects. Consumption
is owner-authenticated and one-use before provider submission; the normal
runner still owns budget, concurrency, credentials, accounting, and evidence.
The controller archives the use against one terminal manifest and passport
charge. Drift, capped loops, active work, replay, publication, approval, merge,
and terminal boundaries remain impossible to override.

## 2026-08-05 — Decision 276: Successors retire only exact expired lease files

Category: Reliability

A parked lease-free successor claim may recover from a prior candidate's stale
lease only after its current passport, route, branch, and remote head are exact.
The sealed helper rechecks expiry and exact lease identity while holding the
ordinary locks, then the controller durably claims a fresh lease. Renewal races,
live runs, malformed or duplicate identities, wrong tickets, and siblings stay
closed; restart observes the completed migration without repeating release.

## 2026-08-06 — Decision 278: Emergency closeout accepts exact idle pause shapes

Category: Reliability

Emergency attestation accepts the idle `blocked`, `claimed`, `waiting`, and
`budget` statuses emitted by `ticket-control pause`; budget pauses require their
exact signed budget digest. A nonblocked lifecycle pause may retain only an
allowed Resume-State overlay. Active or unknown statuses, malformed budgets,
and invalid lifecycle overlays remain closed.

## 2026-08-06 — Decision 279: Emergency terminals preserve qualification truth

Category: Reliability

A successor qualification reconciles an emergency Done receipt against the
unchanged authenticated source passport and exact signed idle pause. It keeps
the historical role and spend evidence, assigns no new provider spend, and
never fabricates Approved/merged history; drift and duplicate reconciliation
remain fatal. A terminal emitted before the consumer repair is admissible only
when its exact Kit-SHA appears in the active qualification environment's
content-addressed receipt chain.

## 2026-08-06 — Decision 280: PR identity follows lifecycle evidence, not branch history

Category: Reliability

Fixed ticket branches retain immutable historical PRs across protected-base
refreshes and qualification migration. The active boundary selects exactly one
open PR; merge detection and Done use the PR number sealed into the approval
attestation and revalidate its branch, base, head, merge, checks, and protected
ancestry. Historical PRs remain auditable without making the current lifecycle
ambiguous, while multiple current candidates or bound-identity drift still
refuse.

## 2026-08-06 — Decision 281: Terminal reconciliation retains its generation boundary

Category: Reliability

The strict protected-terminal reducer consumes the generation and manifest
digest already authenticated by qualification event selection. Both fields
are required; partial or additional event shapes remain invalid. This keeps
zero-cost historical terminal adoption compatible with reused durable
authority without weakening cross-generation isolation.

## 2026-08-06 — Decision 282: Terminal cohorts do not fabricate live-cell proof

Category: Reliability

An all-terminal successor has no runnable cell to relocate. The reducer accepts
zero or one valid relocation only when protected reconciliation, emergency
reconciliation, and terminal adoption cover every selected ticket. Any cohort
with a publication target still requires exactly one current-candidate
relocation; duplicate and foreign-ticket evidence remain invalid.

## 2026-08-06 — Decision 283: Product certification applies Seatbelt per phase

Category: Safety

macOS does not permit a sandboxed process to apply another Seatbelt profile.
The protected product wrapper therefore coordinates only its disposable tree;
the sealed runner launches every declared phase through exactly one mandatory
Factory-generated profile. Filesystem restrictions remain common, denied phases
retain no external network, and only a reviewed optional or required phase uses
the network-enabled profile. Missing or malformed profile bindings fail before
any phase starts.

## 2026-08-06 — Decision 284: Terminal ledgers preserve rows, not byte position

Category: Reliability

Normal and emergency Done receipts bind their immutable closeout ledger, while
current protected truth must retain every attested run ID with the exact same
row. Reordering and new rows are safe; missing, changed, duplicate, malformed,
or schema-drifted rows refuse. Closeout projection seeds durable history from
the exact protected-main worktree instead of a possibly stale runtime checkout,
preventing a concurrent closeout from deleting already-protected accounting.

## 2026-08-06 — Decision 285: Qualification dispatches protected ticket bytes

Category: Reliability

A qualification candidate may carry control metadata, but every selected
ticket blob must equal freshly fetched protected main both before seal and
before claim. No local ticket edit is copied into the execution checkout.

## 2026-08-06 — Decision 286: Reviewer labels tolerate only bounded decoration

Category: State machine

The shared Reviewer parser accepts an exact verdict label with optional heading,
bold emphasis, and terminal period. It does not infer verdicts from prose,
negation, or mixed labels.

## 2026-08-06 — Decision 287: Protected terminal truth precedes retained refs

Category: Reliability

Production activation validates protected Done or lease-free Canceled before
consulting retained ticket refs. Refs remain immutable for lane isolation;
nonterminal protected truth keeps the existing exact lease checks.

## 2026-08-06 — Decision 288: Qualification metadata is production-invalid

Category: Safety

Production certification, planning, and activation reject a product containing
`factory/QUALIFICATION.json` before receipt or journal mutation. Qualification
continues to require the same manifest.

## 2026-08-06 — Decision 289: Linear reconciliation is batched and cooldown-aware

Category: Throughput

Scheduled reconciliation reuses paginated issue and Project inventories and
loads full comments only for a recent changed comment head. Typed quota
responses persist a bounded cooldown that is checked before any Linear access.

## 2026-08-06 — Decision 290: Malformed backlog contracts are ticket-local

Category: Reliability

Ordinary admission skips a malformed dependency contract with named durable
evidence instead of aborting every sibling. Selected qualification contracts
remain cohort-fatal so a sealed run cannot silently weaken its authorized set.

## 2026-08-06 — Decision 291: Null initiatives are visible admission refusals

Category: Reliability

Linear's null initiative remains an authoritative tombstone rather than
falling back to Git. A Ready ticket with no effective initiative is reported
by ID in admission results, events, and incident evidence instead of silently
disappearing from the candidate set.

## 2026-08-06 — Decision 292: Linear Project identity fails before creation

Category: Reliability

One durable initiative marker and mapped Project ID define canonical identity.
Missing mappings, foreign-team Projects, duplicate markers, and same-name
conflicts refuse reconciliation; no heuristic silently creates or adopts a
replacement. Doctor exposes the canonical mapping for operator cleanup.

## 2026-08-06 — Decision 293: Contract resume intent outranks self-writes

Category: Reliability

The Linear blocked baseline is immutable for one substantive blocker and
ignores exact receipt-bound resume lines. A decision already validated for that
same blocker survives a concurrent newer reconciler snapshot. Rejected moves
remain blocked in authenticated Factory truth but are not overwritten in
Linear; their typed reason is persisted and deduplicated.
## 2026-08-06 — Decision 294: Contract-resume refusals are durable and typed

Category: Reliability

Receipt-bound contract recovery keeps its strict authenticated two-line commit.
The controller records receipt mismatch, ambiguous directives, unpushed heads,
invalid ancestry, and over-full content as ticket-scoped durable events; Doctor
folds the latest refusal and recovery event per ticket. Substantive operator
rulings are pushed and migrated first, followed by a separate exact receipt
commit, so the security boundary and the operating instructions agree.

## 2026-08-06 — Decision 295: Preflight fixes rebind idle repairs

Category: State machine

An active ordinary contract repair with no successful owner evidence may move
from its signed head only through one exact authenticated forward passport
migration. The superseded signed record is archived and the active record is
re-signed at current HEAD without consuming another attempt. Dependency,
post-success, cross-Factory, rewritten, missing, or ambiguous lineage remains
closed with a typed head-moved refusal.

## 2026-08-06 — Decision 296: Opus is a new default profile, not rewritten history

Category: Model policy

New no-record routing uses `cursor-opus-v1`, selecting exact Cursor Opus 5
medium-thinking routes for Spec-linter and Test-author with native Claude Fable
as the same-family fallback. `cursor-balanced-v2` and its Fable route remain
unchanged so existing activations and pinned ticket plans retain their exact
catalog, profile, and route identity.

## 2026-08-06 — Decision 297: Delayed terminals are reduced, never replayed

Category: Reliability

An idle `missing-terminal` claim may recover only its exact current-kit,
role-matching terminal through ordinary reduction. Missing or mismatched
evidence remains blocked without another provider launch or charge.

## 2026-08-06 — Decision 298: Preflight recovery is lane-neutral

Category: State machine

The safe passportless Planner-preflight recovery contract applies in
production and qualification. Qualification adds its sealed selection checks;
the authenticated clean-cell, current-route, no-run, and lease invariants are
shared.

## 2026-08-06 — Decision 299: Linear cooldown follows credential identity

Category: Throughput

Rate-limit state is stored in an owner-only credential-hash namespace so all
new reconcilers sharing one account stop before API access. Per-project health
remains for diagnostics, and older reconcilers must stay unloaded or isolated.

## 2026-08-06 — Decision 300: Qualification validates strict contracts before admission

Category: Reliability

Selected tickets pass the existing ticket-readiness validator before a sealed
lane is created. Invalid product-decision, dependency, fixture, authentication,
or protected-test declarations fail before any mutable runtime or provider work.

## 2026-08-06 — Decision 301: Provider identities stay exact and current

Category: Model policy

The Cursor Opus route binds the observed `Opus 5 300K Medium` identity rather
than the obsolete 1M display label. Native Claude fallback defaults to its
certified 2.1.223 CLI. Different model labels and version drift still fail
closed; no fuzzy aliases or version ranges are accepted.

## 2026-08-06 — Decision 302: Qualification owns its model configuration

Category: Safety

Qualification preparation snapshots an owner-only global model configuration
under the isolated root, and its sealed launcher passes only that path to
helpers. Production configuration changes cannot drift an active qualification
lane; replacement is allowed only through a drained upgrade boundary.

## 2026-08-06 — Decision 303: Native Claude readiness matches isolated execution

Category: Safety

Native Claude version, help, OAuth, and authenticated-status probes run through
one disposable owner-only configuration populated from a securely validated
credential copy. Ambient Claude hooks and settings cannot strand fallback, and
the probe removes its credential copy on every outcome.

## 2026-08-06 — Decision 304: Exact model-identity success is preserved

Category: Reliability

When an old Cursor catalog rejects a completed Spec-linter only because its
reported model label now matches the successor catalog, recovery authenticates
the single terminal success, exact ticket-only output/revert history, and a
bounded contiguous route-migration chain ending at the current kit. The
controller reapplies only the exact ticket append on top of that chain, exports
one charge and one completion
record, and never replays the provider. Any broader history or evidence shape
stays blocked.

## 2026-08-07 — Decision 305: Typed recovery is reachable through the sealed launcher

Category: Reliability

The sealed launcher admits the controller-only
`passport verify-model-identity-success` grammar with exact ticket, receipt,
run, worktree, and JSON arguments. Missing or reordered evidence remains
refused before the passport helper runs.

## 2026-08-07 — Decision 306: Model-identity recovery replays an exact ticket delta

Category: Reliability

A completed Spec-linter output need not be append-only. Successor recovery
authenticates its ticket-only output and exact revert, then requires a
conflict-free three-way replay of that same delta across the route-migration
tail. Active selection resolves from the newest journal revision that carries
resolution data because unchanged release revisions may omit it. Conflicts,
changed replay content, extra commits, and ambiguous topology remain blocked.

## 2026-08-07 — Decision 307: Release normalization preserves typed recovery

Category: Reliability

Typed old-release terminal recovery accepts blocked claims and the idle claimed
or running forms produced by release-upgrade normalization. An active role is
never recovered. The same exact receipt, terminal, passport, Git, and remote
evidence remains required before clearing the pending role; ordinary provider
fallback cannot consume that preserved run.

## 2026-08-07 — Decision 308: Corrected completions retain immutable artifacts

Category: Reliability

Qualification artifact closure binds each authenticated completion correction
one-to-one to its completed evidence. It preserves the original failed terminal
bytes, accepts only the correction type's exact terminal status, and retains the
signed progress identity. Uncorrected or mismatched failed manifests remain
ineligible for stage sequencing.

## 2026-08-07 — Decision 309: Protected role refusals are recoverable, not accepted

Category: Reliability

A protected ticket-field mutation remains an exit-11 failure and is never
pushed. Clean Planner, Spec-linter, Builder, and Narrator outputs are preserved
under a diagnostic ref while the exact remote input is restored; only a
successor with one authenticated failed charge and no completion may retry the
role. Legacy occurrences that exported a passport at the rejected head first
use the existing protected in-flight rewrite authorization to return to the
unchanged remote input.
