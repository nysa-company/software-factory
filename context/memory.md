# Memory — software-factory decision log

Entry format: `## YYYY-MM-DD — Short title`, then `Category: Decision | Preference | System change | Context`, then 1–3 sentences. Mark superseded entries clearly and number durable decisions within this repository.

## Current truth

- This repository is a product-agnostic factory kit; product repositories carry only their factory state and CI integration.
- Durable decisions in this repository use their own numbering, beginning at Decision 1.
- Model routing is portfolio policy: the catalog separates transport, gateway, inference provider, family, account route, selection ID, and reported identity; profiles order all-six-role portfolios with distinct production/checking families. Routes are pinned at the ticket boundary and may change mid-ticket only through the Contract 1.4 one-use Linear-approved journal flow; one logical role attempt submits to at most one process.
- Stable product and operating truth lives under `docs/`; executable prompts, copied templates, conformance evidence, and vendored material remain colocated with their consumers.
- The repository adopts Nysa Agents baseline v3 as a toolkit with repository, secret, artifact, Git-flow, CI, and config-review enabled. `bash ci/test-all.sh` remains the unconditional full command; managed local readiness uses `bash ci/test-all.sh --changed-or-defer origin/main HEAD`.
- Dynamic CI selection is fail-closed and evidence-gated: six audited leaf components remain available for focused local and pull-request work. Broad readiness and pull requests run policy gates and defer complete coverage; every `main` commit runs the complete registry in three Linux and three macOS shards.
- Live products resolve sealed exact-SHA kit releases under `~/.factory/kits` through the stable `~/.factory/bin/factory-launch` contract; kit merges are candidates until a product-specific certified activation.
- Install records owner-only, expiring kit-suite evidence for the exact sealed release. Exact protected-main GitHub Actions full-suite evidence is mandatory and is followed by a sandboxed platform smoke; missing evidence fails closed without a local full fallback. Certification reuses evidence only when every release, physical-tree, host, platform, suite-definition, tool-version, source, and configured-lifetime binding matches; product certification and binding checks always rerun.
- Product certification stages a checksum-verified pinned scanner into the disposable product copy before entering its network-denied sandbox, so managed secret scanning never depends on a prewarmed product cache or certification-time network access.
- External products require one full `factory/KIT_PIN`, and the first role launch records a durable ticket `Kit-SHA`; only the in-repository conformance test bed has an implicit runtime pin.
- Release activation is maintenance-gated, receipt-bound, and journaled. Failed-cutover recovery keeps `MAINTENANCE`, stops product factory services, reconciles any interrupted transaction, restores the protected previous pin/tree, and calls rollback only for a committed active candidate; automatic pruning is intentionally unavailable.
- The required aggregate `ci` status always reports. Pull requests retain policy and applicable targeted checks; every merged SHA runs complete Linux and macOS verification before it can become a release. Relay generation 4 runs documentation-only release `35c2e10` with healthy generation 3 on `3b63cc7` retained as its exact current-tree rollback baseline; the five-minute outage target and formal rollback RTO remain unaccepted.
- On macOS hosts where `/usr/bin/python3` is an xcrun shim, the launcher and release sandboxes use the fixed Command Line Tools Python binary when available; this preserves default-deny Seatbelt behavior without xcrun cache writes outside the sandbox.
- The no-record default is `cursor-balanced-v2`: it preserves `balanced-v2` models and effort levels while trying the matching Cursor route before native Codex/Claude CLI. Explicit active profiles and committed ticket route plans remain authoritative; `balanced-v2`, `legacy-balanced-v1`, and the earlier priority profiles remain available for compatibility.
- Cursor adapters append a trusted execution requirement after role and task text so factory roles stay in the default agent execution mode instead of switching to Cursor Plan or Ask mode.
- Parallel kit branches, worktrees, PRs, and inert candidate releases are supported. Product activation/rollback remains serialized; contracts 1.1 through 1.5 default to one ticket and permit at most four exact-worktree ticket leases, while Contract 1.6 defaults to four and permits at most six. `MAX_CONCURRENT_TICKETS` is the single coupled worktree/provider capacity setting. Contract 1.6 may bypass the product-wide provider lock only for an exact activation-gated API route executed through the isolated runtime, broker, networkless worker, and trusted artifact controller. Native subscription and Cursor CLI routes retain the legacy serialized path; invalid or disabled activation fails closed to that path.
- Spec-linter and Reviewer escalation overrides accept only an exact authorization for the next semantic round. Test immutability treats `.gitignore` and `context/memory.md` as exact-file bookkeeping exemptions, while documentation remains contract-significant; revert branches use `chore/<slug>-revert`.
- Ticket execution reads Git-authored state from the exact ticket worktree/committed branch and overlays Linear-owned fields from ignored `factory/linear-map.json`. Mutating roles must commit cleanly; the trusted wrapper non-force pushes and verifies them, while Reviewer must leave Git unchanged.
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
- Factory development may use the macOS-only disposable lane against an exact clean commit, a synthetic product, and a local Git remote. Mock mode is network-denied under outer Seatbelt and must finish below 15 minutes; real Cursor uses the authenticated CLI session, Cursor's explicit internal sandbox, and a one-use executable/session-bound approval because the current CLI cannot authenticate inside nested Seatbelt. Only Cursor subprocesses receive the session home, and Cursor's hardcoded temporary root is redirected into the lane for each invocation. Lane artifacts have no release receipts or activation records, and the production release contract is unchanged.
- A development Cursor lane may reclaim its hardcoded temporary bridge only when it is an empty owner-owned directory tree with no group/other write permission and all subscription providers are idle. Files, symlinks, content, unsafe ownership or modes, and active providers refuse; the replacement remains an atomic lane-target symlink with exact-owner cleanup.
- Subscription and product development lanes copy CLI session files once, then bind readiness, version evidence, approval hashes, and role execution to the same sanitized lane-local session environment and working directory. Ambient authentication variables, caller working directories, and external Cursor session state cannot satisfy readiness; unavailable copied authentication stops before approval consumption, lease claim, reservation, or task submission.
- Development-lane subscription readiness retries three times with a one-second delay between misses. This absorbs short lane-local CLI session transitions while preserving the same fail-closed pre-approval boundary.
- Retained-product resumes stabilize lane-local subscription readiness before planning and before execution validation. The internal run reuses that execution proof, while each pinned role still verifies its exact route before reservation or task submission.
- The development product scheduler never retries a failed role automatically. A drained failure reports the exact ticket set and hands control to `product-resume-plan`, which revalidates the retained lane and issues a fresh one-use approval before the same mechanical stage may run again. Targeted resume resolves stages and takes leases only for selected tickets while hash-binding every original sibling's clean Git and evidence state; later resumes may choose another original subset.
- The disposable lane also has a four-ticket mock-concurrency mode. It reuses the transactional coordinator and CLI runtime with one activated mock account, proves four-way provider overlap and reservation drain, then completes four synthetic role lifecycles; every runtime input must resolve beneath the validated owner-only lane root.
- Fresh isolated product proofs still require four tickets; a seeded retry may select one to four unfinished tickets from an owner-only bundle. Its accounting manifest binds the exact bundle and base, carries the full historical reservation map, and reduces both selected-ticket and aggregate lane budgets. The trusted helper derives a stable lineage identity from that base and full ticket set; its owner-only record atomically advances one shared cumulative manifest head, preventing two fresh lanes from choosing different IDs or spending from the same parent snapshot. V2 retains $100/$500 defaults; operator-authored v3 permits only the explicitly approved $200/$700 development ceilings and consumes one nonce on one fixed UTC day before lane creation. Canonical product sources, duplicates, seeded symlinks/submodules, stale accounting siblings, and partial lease-claim residue fail closed.
- Seed accounting CAS is consumed only after lane construction and planning succeed, while approval output is still withheld. A construction failure retains an unconsumed diagnostic lane; a CAS loser is cleaned without exposing a runnable root.
- A drained Contract 1.7 development lane may export a one-use pre-Reviewer checkpoint to a newer development kit. Only exact successful Planner-through-Builder prefixes are carried; failed attempts remain charged but are rerun, omitted tickets restart at the source boundary, Reviewer and Narrator always rerun, and v5 seed accounting atomically binds the checkpoint plus the full historical ticket spend. Import recognizes the trusted model pin's atomic route-plan plus ticket `Kit-SHA` commit, discards that stale control evidence, writes the current development kit binding, and retains an exact owner-only checkpoint copy so another corrected-kit export can prepend the prior role records after verifying its digest, import binding, and head ancestry.
- Imported checkpoint Spec-linter verdicts are an immutable prefix, not the complete future ticket history. Current-lane verdicts may extend that prefix only when the ordinary current-lane ledger count authenticates each addition.
- Development product export projects only the latest successful Reviewer's non-`factory/` tree changes and rejects later product drift. Exact bundles retain detailed role/retry/audit history but are never applied to canonical product branches; the canonical mailbox deterministically emits one pure final-test commit before one pure implementation commit with the same reviewed application tree. Unsafe or empty strata fail closed, preventing lane controls, route pins, sibling tickets, and unreviewed Narrator metadata from escaping.
- Contract 1.7 adds owner-activated subscription-CLI concurrency through the existing transactional coordinator. Activation v2 binds the exact allowed CLI route tuple and canonical provider-policy digest, permits Codex account capacity through four, and keeps Cursor and native Claude capped at two; Contract 1.6 accepts only API activation v1, and missing or invalid activation remains serialized. The marker-bound development lane may use coordinator-owned bounded pre-GO waiting for transient concurrency denial for up to fifteen minutes while its lease heartbeat runs and the product launch lock is released; budget and permanent denials remain immediate. Role instructions use worktree-relative database-environment paths, and a shared fail-closed sentinel rejects newly added absolute `nysa-sf-dev.*` paths before trusted-host push and at both checkpoint boundaries. Seed import authenticates the old linear lane-control boundary before excluding its Factory-owned path rewrite from that scan; every later route and role commit remains scanned, and replay preserves the new lane's configuration.
- The PR-less development Narrator accepts frozen backend HTTP contracts with canonical backend-only N/A evidence or with Preview explicitly pending/unavailable until the PR/deploy gate and Screenshots explicitly unavailable because there is no UI or visual surface. Its bundle is development-only and not a production attestation.
- The Contract 1.7 development scheduler authenticates durable contract-blocked role manifests before moving only that ticket to Blocked-Escalated. Its lease drains, siblings continue, and the blocker is excluded from ordinary resume instructions.
- Isolated broker-stage cancellation and deterministic failure release capacity only after token revocation and upstream-request drain are both proven; otherwise the conservative full reservation remains active. Executor success is not durable until bound-container removal succeeds.

## Log

- 2026-07-24: Reviewer repairs can interleave Test-author and Builder commits, so replaying role history as the product mailbox violated tests-first CI despite an approved final tree. Development export now keeps detailed role history in its exact bundle while deterministically projecting the reviewed application tree into one pure final-test commit followed by one pure implementation commit; unsafe or empty strata and tree drift fail closed.
- 2026-07-24: Cursor aggregated one valid Reviewer assistant response into its terminal result twice, causing strict Contract 1.7 repair ownership to appear duplicated. Reviewer parsing now prefers exactly one terminal-bound verdict assistant while rejecting ambiguity or contradiction, and retained development lanes reconcile through the validated invoking controller instead of their stale pinned helper.
- 2026-07-24: Cursor can leave its hardcoded scratch bridge as a safe empty directory tree after replacing the lane symlink, which made the next development plan fail before GO. Preclaim now reuses the existing fail-closed empty-tree validator only after subscription-provider idle proof, then atomically claims the bridge as the lane symlink; every unsafe or active shape still refuses.
- 2026-07-24: Seed checkpoint import scanned from the pristine product base and rejected an authenticated prior lane-control `PROJECT.env` path as provider output. Import now proves linear ancestry and the exact first-commit lane-control identity/path scope before scanning only later commits; provider-added stale paths still fail, and the old control commit is never replayed over the new lane configuration.
- 2026-07-24: A corrected controller could not export an older pinned retained lane because it looked for the newly introduced lane-path sentinel inside that old kit, and targeted exports then rejected or forgot the full original-ticket charge universe. Checkpoint boundaries now keep exact lane pin validation while running the trusted invoking controller's sentinel; selected records may retain a superset charge map, and repeated chaining derives that universe from the retained checkpoint while remaining exactly bound to v5 accounting.
- 2026-07-24: Checkpoint export initially scanned from the pristine product base and therefore mistook the Factory-owned lane-control commit for provider output. Export path scanning now begins at the recorded `lane_control_sha`, leaving seed validation at the pristine base and scanning every role commit.
- 2026-07-24: A current-lane Spec-linter verdict after a Planner-only checkpoint was incorrectly rejected because sequencing compared the evolving ticket verdict list to the complete checkpoint list. Checkpoint verdicts now remain an exact immutable prefix, while the existing ledger-to-verdict reconciliation authenticates later current-lane additions.
- 2026-07-24: A drained four-ticket lane exposed that targeted resume still resolved every original sibling and required excluded tickets to be complete, so one blocked contract prevented an unrelated Reviewer retry. Resume now validates runnable stages only for the selected subset while binding every original sibling's clean head, origin, tree, ticket, route, envelope, and evidence; subsequent attempts retain the original selection universe and excluded drift fails closed.
- 2026-07-24: A retained Planner contract copied its first lane's physical database-environment path and a third Cursor call timed out twelve seconds before capacity released. Development roles now receive a portable worktree-relative database path, role push and checkpoint import/export reject added absolute lane paths, and coordinator-owned transient-capacity waiting uses its existing fifteen-minute bound without changing budget, cancellation, or production routing.
- 2026-07-24: The first real cross-kit checkpoint retry exposed that its importer expected a route-only commit even though the trusted model manager atomically commits the route plan and ticket Kit-SHA. Import now validates that exact two-file shape, proves Kit-SHA is the only ticket change, discards the stale pin, and writes the current development-kit binding.
- 2026-07-24: That same retry showed seed authorization was consumed before lane construction, so a fail-closed import error could burn a zero-provider reservation. Planning output is now withheld until construction succeeds and the lineage CAS is consumed; failed construction remains retryable, while a CAS loser is cleaned before any runnable root is exposed.
- 2026-07-24: Checkpoint planning then exposed that the non-provider helper environment omitted the validated lane root, so exact checkpoint stage reproduction refused while direct subscription helpers passed. All trusted development product helpers now carry the same lane-root and internal-sandbox binding used by provider execution.
- 2026-07-24: A second corrected-kit retry exposed that checkpoint export saw only current-lane manifests and discarded imported successful roles. Import now retains the exact source checkpoint, and export validates its hash/import/head bindings before chaining the prior records ahead of current successes.
- 2026-07-23: A real four-ticket pilot exposed that a Builder could correctly commit an impossible frozen-contract blocker while the development scheduler treated it as a generic resumable failure. Contract 1.7 now authenticates that durable wrapper result, transitions only the affected ticket to Blocked-Escalated, drains its lease, and continues siblings without replay.
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
