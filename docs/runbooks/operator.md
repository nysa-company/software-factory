# Factory operator runbook

What to do when something breaks, written for a non-technical operator. Each entry: how you notice, what to do, what not to do.

## Stuck ticket (no movement for hours)

- Notice: ticket sits in an active role column with no new commits or comments.
- Do: check the terminal/session running the role. If it's spinning or confused, stop it, add a ticket comment "run abandoned — restarting", and re-run the role through `~/.factory/bin/factory-launch <project> run`. Second stall on the same ticket → move it to Blocked-Escalated and re-read the ticket's contract: stalls usually mean the spec is ambiguous.
- Don't: let a stuck run keep burning budget while you wait.

## Runaway spend

- Notice: daily spend rollup jumps, or a provider console alert fires.
- Do: run `scripts/kill-switch.sh` immediately (safe — it stops, it doesn't break anything). Read `factory/runtime-ledger.csv` for today's effective rows and find the expensive role/ticket; tracked `factory/ledger.csv` is close-out history and may be stale during a live incident. That ticket goes to Blocked-Escalated; resume the rest by removing `factory/KILL`.
- Don't: rotate API keys for a spend problem — that's for leaks, and it kills your own sessions too.

## Failed deploy / broken staging

- Notice: staging URL errors, or a Narrator bundle says "preview broken".
- Do: check Railway's dashboard for the failing deploy log; the usual fix is reverting the last merged PR per `docs/operations/rollback-drill.md`. If staging is down but no recent merge happened, restart the Railway service from its dashboard.
- Don't: approve anything while staging is broken — bundles can't be verified.

## Leaked secret (a key appears in a file, log, or commit)

- Notice: a scanner alert, a reviewer comment, or you see a key string somewhere it shouldn't be.
- Do: this is the one case for rotation. Revoke the exposed key in the provider console, issue a new one, update GitHub/Railway secrets. If it reached git history, treat the repo history as public: rotate everything that repo ever saw. Then file a ticket to fix the path it leaked through.
- Don't: just delete the file and move on — the key is still burned.

## Model provider down (Claude or OpenAI outage)

- Notice: runs fail immediately with API errors; provider status page confirms.
- Do: if the primary is positively unavailable during the wrapper's non-task probe and a calibrated same-family Cursor fallback is enabled, the wrapper selects it before submitting the task. An outage discovered only after task submission is a failed run: stop, inspect the run manifest, and wait or make a new operator-authorized attempt. If agents are scheduled, `scripts/kill-switch.sh` remains the safe stop.
- Don't: manually swap families or relaunch after a post-submission failure. Cross-family separation and one-agent-per-logical-run are quality controls, not conveniences.

## Duplicate reviewer row

- Notice: the launcher's `next-stage` route refuses because successful reviewer runs outnumber verdicts, and the extra row came from an overlapping duplicate rather than a real review round.
- Do: count successful reviewer rows for that ticket from oldest to newest. Add `OPERATOR NOTE: reviewer run <N> void — duplicate` to the ticket, using the duplicate row's one-based number. Re-run the launcher's `next-stage` route with the active contract's argument grammar. The next reviewer round number comes from recorded verdicts, so the void row does not renumber it.
- Don't: invent a verdict for the duplicate row or delete ledger history.

## Live or unreconciled run claim

- Notice: launch refuses with `live or unreconciled run claim exists`, or a run reports control-plane mutation and leaves `factory/.active-runs/<ticket>.<role>.lock` behind.
- Do: publish maintenance first with `bash scripts/factory-kit.sh pause --project <project> --product <absolute-product-path>`. Check that the claim and its `owner` are real, non-symlink directory/file entries. Read only the recorded `pid` and `process_start`; compare both with `ps -o lstart= -p <pid>`. If the PID is live with the exact recorded start value, the claim is live and must remain. If the PID is absent or its start differs, confirm no recorded `factory/runs/*.pid` process is live, then quarantine only that exact claim by renaming it to `<claim>.stale-<UTC timestamp>`. Re-run doctor and preflight while maintenance remains published.
- Don't: infer staleness from a PID alone, delete every claim, reclaim during ordinary launch, print the owner token, or remove maintenance before accounting and run health agree. A claim is never stolen automatically.

## Global accounting or wrapper control-state mutation

- Notice: the role exits with `role_exit_control_plane_mutation`, the global ledger lock remains, or the wrapper says operator reconciliation is required.
- Do: keep maintenance published. A product-level control lock normally permits only one provider interval, so quarantine and reconcile every new or changed sibling manifest before another launch. Compare the affected run manifest and conservative reservation with the provider console, validate that the global ledger is a regular non-symlink CSV with the expected header and nonnegative rows, and retain the full reservation wherever cost is uncertain. When the wrapper still owns the exact global lock it restores its pre-provider snapshot; changed lock ownership or unresolved ledger state requires manual reconciliation before another launch.
- Don't: hand-edit or delete ledger history, remove an unknown-owner lock, or treat a reconstructed `.out` artifact as accounting authority. Provider output is captured by the wrapper and telemetry is only accepted when bounded and parseable.
- Important: persistent mutation is detected and blocks advancement, but an unsandboxed provider shares the launcher's OS user. Preventing a hostile same-UID process from changing and restoring user-owned state requires OS isolation such as a separate UID or enforced sandbox; file snapshots and `mkdir` locks are not that boundary.

## Stale provider lock

- Notice: launch refuses with `stale provider lock requires operator reconciliation`, or doctor reports `provider_lock_state=stale` or `malformed`.
- Do: keep KILL published and run `scripts/kill-switch.sh`. After recorded process groups drain, it quarantines only a safe, unchanged owner whose PID is absent or has a different process start identity. Re-run doctor and preflight, reconcile manifests and active claims, then remove KILL only when accounting and control state agree.
- Don't: delete or rename `factory/.provider.lock` by hand. A live, malformed, changed, symlinked, hard-linked, or otherwise ambiguous lock is deliberately retained for inspection; ordinary launch never steals it and the ownership token must not be printed.

## Spec-linter or reviewer reached the two-round limit

- Notice: the launcher's `next-stage` route returns `ESCALATE` and names the next semantic round.
- Do: if one more cycle is warranted, append exactly `OPERATOR AUTHORIZATION: spec-linter round <N>` or `OPERATOR AUTHORIZATION: reviewer round <N>` using the round named by the sequencer, then run it again.
- Don't: add commentary to the authorization line, authorize a future round, or let the dispatcher infer authorization. A stale or inexact line grants nothing.

## Linear, GitHub, or Railway down

- Do: if Linear is down, in-flight factory work continues from the ticket files, but do not expect a new priority, Ready, approval, or unblock action to take effect until sync recovers. Check `_sync.last_success_at` and `_sync.last_error` in `factory/linear-map.json`. GitHub or Railway outages still pause the stages that depend on them.
- Don't: edit factory-owned Linear descriptions or force local state to imitate an operator transition that has not been ingested.
- Note: on a new product, the first normal Linear reconciliation may durably initialize the missing real `factory/runs/` directory. A file, symlink, or other invalid entry at that path is an integrity failure, not something sync replaces.

## Broken connector (external sends failing)

- Notice: tickets with the `external` label fail their sends; receipts/error comments show it.
- Do: confirm the connector's sandbox/production mode and its credentials in the product's settings. Flip nothing to production while debugging. Escalated failures are a ticket for the factory, not a manual workaround.

## Restore from backup

- Postgres (staging): Railway dashboard → database → Backups → restore. Staging data is disposable; fixtures re-seed it.
- Board: restore the product repo first. Markdown and the ledger are the durable execution record; `scripts/linear-sync.py --setup` plus a normal sync recreates Projects/issues and mappings. Linear remains authoritative only for operator-owned priority, Project membership, Ready, approval, and unblock actions.

## Model portfolio control

The operator owns profile activation and temporary credit-exhaustion overrides.
Use only the selected release through the sealed launcher:

```bash
~/.factory/bin/factory-launch <project> models profiles --json
~/.factory/bin/factory-launch <project> models plan --profile openai-priority-v1 --json
~/.factory/bin/factory-launch <project> models activate --profile openai-priority-v1 --approve-hash <preview-profile-hash> --approved-by <operator-id> --json
~/.factory/bin/factory-launch <project> models disable --scope-type account-route --scope-id codex-native --reason credits_exhausted --ttl-seconds 3600 --operator-id <operator-id> --json
~/.factory/bin/factory-launch <project> models enable --scope-type account-route --scope-id codex-native --json
~/.factory/bin/factory-launch <project> models pin --ticket T-123 --workdir /absolute/ticket-worktree --json
```

`models plan --json` previews the active profile, or default
`legacy-balanced-v1` when none is active. Activation accepts only the exact
profile hash shown by preview. `openai-priority-v1` tries OpenAI production
first, then Anthropic production; `claude-priority-v1` reverses those
portfolios. `cursor-priority-v1` tries exact Cursor OpenAI/Anthropic routes
before native routes in both portfolio orders. Legacy has one native-first
OpenAI-production/Anthropic-checking portfolio.

Use `disable` only for confirmed temporary credit exhaustion. It can target an
account route, provider family, selectable model ID, or exact route for 1 to
604800 seconds; expiry is automatic, while `enable` removes it early.
Subscription quota telemetry is incomplete, so the launcher cannot reliably
infer remaining credits or distinguish every subscription limit from an
outage. Record the operator judgment and choose the narrowest scope.

Pin only on the clean exact ticket worktree before the first role. It records
the Kit-SHA and exact six-role route plan in one commit, pushes and verifies the
ticket branch, and is idempotent for the same committed plan. Roles never
re-resolve. Each run re-probes only its exact pinned route, and any failure
after task submission ends the run without retry.

Kimi K2.6 remains disabled experimental through Claude CLI/OpenRouter/Moonshot.
No live or billed pilot has run. Rotate the credential before a pilot; direct
same-UID token exposure remains until a broker or OS isolation is used.

## Preflight failed before launch

- Notice: the dispatcher escalates with `PREFLIGHT FAIL` output from the launcher's `preflight` route — no pinned safe route, adapter contract/version mismatch, budget headroom, git state, or ticket not Ready.
- Do: read each FAIL line. Common fixes: inspect `models status --json` and `models plan --json`; run `scripts/adapters/contract-test.sh --routes`; reconcile pinned CLI versions in `~/.factory/global.env`; authenticate the affected account route without printing credentials; raise `DAILY_CAP_USD` or `GLOBAL_DAILY_CAP_USD` if the projected reserve no longer fits; clean and sync the repo to `main`; confirm the ticket is Ready. Re-run preflight through `~/.factory/bin/factory-launch <project> preflight` before resuming.
- Don't: tell the dispatcher to launch anyway — every FAIL is predictable at kickoff and will block mid-pipeline.

## Trusted approval and close-out PR

- Notice: under contract 1.3, move only Awaiting Approval → Approved in Linear after reviewing the exact bundle. The trusted approval action commits the binding and requests protected GitHub auto-merge. If it refuses stale evidence, a changed head, conflicts, unavailable auto-merge, or failed checks, investigate the named condition; never manually imitate the attestation.
- Notice: after the ticket PR merges, the dispatcher opens `chore/tNNN-closeout` from current `origin/main` and invokes `ticket-attest --action done`. It verifies the merge and configured post-merge contexts, projects the ledger once, commits Done plus closeout evidence, creates or reuses the exact factory-owned closeout PR, and requests protected auto-merge. No operator approval or manual GitHub merge is required. After protected main contains valid Done, sequencing returns `COMPLETE` and the dispatcher releases the lease. Projection refuses any active or ambiguous claim.
- Do: reconcile claims and PID records under maintenance before retrying; never delete one based only on its age. Confirm the factory-owned close-out PR entered protected auto-merge; do not supply another business approval or manual merge.
- Don't: edit rows by hand, project while any ticket has a live or ambiguous run, or commit the runtime ledger itself.

## One-time Contract 1.2 legacy closeout

- Notice: this is a bounded release migration, not a permanent compatibility
  mode and not a substitute for Contract 1.3 attestations. Never create
  `factory/attestations/` records for historical work or rewrite its old
  `Kit-SHA`.
- Do: finish and settle every allowlisted old-contract ticket first. Generate
  from an exact clean `HEAD == origin/main` basis with
  `scripts/legacy-closeout.py --product <repo> --request <reviewed-request>`.
  Review the exact repository, source/target kits, ticket classes, immutable
  ticket/bundle blobs, merged PR metadata, four app-bound successful checks,
  ledger rows, cutoff, branch observation, and protected-main basis.
- Do: use `legacy-reviewed` only for exact Review. T-019/T-020 are the only
  permitted `out-of-band-merged` Planning anomalies and require a fresh
  independent audit and combined full-test digests. A historical ticket has
  no route plan and the legacy receipt must say so.
- Do: put authorization, the complete receipt set, Done/Migration ticket
  projections, and target `KIT_PIN` in one product PR. Disable auto-merge and
  bypass; the operator's manual protected merge of that exact head is the
  present approval. Any main movement, conflict edit, changed ruleset, missing
  ticket, failed check, or accounting ambiguity requires regeneration and
  recertification.
- Don't: commit or push from the generator, accept a partial batch, hand-edit
  generated JSON, or treat `State: Done` alone as terminal evidence.
- Rollback: for an interrupted activation use `reconcile`. After a committed
  activation, keep maintenance, merge the protected exact-tree product revert
  restoring the old pin and migration state, then run `factory-kit rollback`,
  restore the prior profile bundle, restart, and verify health before clearing
  maintenance.

## Test commit order before operator review

- Notice: reviewer approved but CI fails the test-immutability gate because test commits came after implementation.
- Do: ensure `scripts/reorder-test-fixes.sh` is present (branch `kit/reorder-test-fixes` in the kit repo). The dispatcher runs it at AWAIT-OPERATOR before opening the PR. If the script is missing, merge or cherry-pick it from that branch first.
- Don't: waive the immutability gate or ask the builder to edit tests post-implementation.

## Parallel kit work while production is running

- Notice: several kit features are being developed while a product remains on
  an older active release. This is expected; branches and merged candidate SHAs
  are inert.
- Do: give each feature its own short-lived branch, linked worktree, protected
  PR, and full verification result. Install and certify each merged SHA
  independently. Canary compatibility-sensitive candidates.
- Do: serialize every product `KIT_PIN` change, activation, and rollback. Begin
  only at a ticket boundary with no active run, no conflicting nonterminal
  lease, no maintenance anomaly, and no incomplete activation journal.
- Do: keep contract `1.0.0` and default contract `1.1.0` at one live ticket.
  An explicit `MAX_CONCURRENT_TICKETS=2` pilot uses one dispatcher holding two
  matching leases; parallel kit development alone does not enable it.
- Don't: pull kit `main` into Sofia's live runtime, run from a mutable checkout,
  combine unrelated candidates into one unreviewed release, or overlap two
  activation/rollback operations.

## Upgrading the kit when multiple products run on it

- Notice: kit `main` moved while products remain on older immutable releases.
  This is normal. A merge is a candidate, not a deployment.
- Do: upgrade one product at a time. Install the exact merged SHA, update that
  product's full `factory/KIT_PIN` through its protected PR, certify the exact
  kit/product tuple, run the required canary, enter maintenance, drain, plan,
  and activate. Follow [../hermes-integration.md](../hermes-integration.md).
- Do: keep every other product on its own active generation. The stable
  launcher resolves releases per project.
- Don't: run live work from the mutable kit clone, replace `factory-launch` as
  part of an ordinary activation, delete or abbreviate the pin, patch an
  installed release, or upgrade all products with one untested pin change.

## Release doctor reports warning or error

- Notice: `~/.factory/bin/factory-launch <project> doctor --json` returns a
  warning/error status, stale Linear sync, a pin mismatch, maintenance, a
  lock, active/stale run records, or unsupported Hermes/CLI information.
- Do: treat `error` as a dispatch stop. A warning requires operator review,
  not automatic repair. Confirm the selected full SHA, physical release,
  `KIT_PIN`, maintenance state, run PIDs, CLI versions, and Linear freshness.
  Credential results are presence-only; test authentication separately
  without printing values.
- Don't: expect doctor to repair, authenticate, clear locks, kill processes, or
  rewrite configuration. It is deliberately read-only.

## Preparing and activating a release

1. Confirm the candidate full SHA is on `origin/main` and the required
   aggregate `ci` check passed on Linux and macOS.
2. Run `factory-kit.sh install`, then `certify`. Record the receipt ID and
   expiry.
3. Complete the real-Hermes canary with a separate sandbox product and
   profile. Never copy the production `.env`, secrets, board mapping, registry,
   ledger, or LaunchAgent.
4. Confirm no active runs and no nonterminal ticket with a different
   `Kit-SHA`. Activation scans committed local, tracking, and live remote
   ticket sources; a Done claim also requires a valid normal attestation chain
   or protected-main legacy closeout.
5. Merge the product's candidate `KIT_PIN` through the protected PR and verify
   the merged product tree still matches the receipt.
6. Publish managed maintenance with `factory-kit.sh pause` before touching
   `.launch.lock`. New claims and renewals stop while matching owners may
   release; wait for existing runs and every dispatcher lease to drain. The
   run wrapper checks maintenance before taking the lock, after taking it, and
   before GO.
7. Run `factory-kit.sh plan`. It must report `No files were changed.`
8. Stop only the product factory profile and reconciler. Leave the dashboard
   and primary Hermes profile alone.
9. Run `factory-kit.sh activate`, restart the factory services, then collect
   doctor JSON, sandbox smoke, PID, Linear freshness, and repeated health
   probes.
10. Remove `MAINTENANCE` only after every acceptance check passes.

If a dispatcher lease is stale, keep maintenance published and run
`factory-kit.sh recover-lease --project <project> --product <path> --ticket
<T-NNN>`. Recovery refuses while any role run is recorded. Never delete or
replace the lease by hand.

The planned control-plane outage starts when maintenance is created and ends
when maintenance is removed. Target: 5 minutes or less. Candidate preparation,
certification, and canarying happen before maintenance and should cause no
outage. Relay generation 1 did not establish this target; its activation-to-clear
interval alone was 5m50s and its full maintenance interval was longer.

## Interrupted activation

- Notice: activation exits before `committed`, `status` reports an interrupted
  transaction, or a journal's latest phase is not terminal.
- Do: keep `MAINTENANCE`, do not hand-edit `active.json` or the journal, and
  run:

  ```bash
  bash scripts/factory-kit.sh reconcile \
    --project "<project>" --product "<absolute-product-path>"
  ```

  Reconcile rolls back phases before `activation_record_switched`. At or after
  the switch it commits only when the active generation and receipt still
  validate; otherwise it restores the previous generation. Restart the
  factory-only services and rerun doctor/sandbox health afterward.
- Don't: clear maintenance, delete the journal, move an activation record, or
  rerun activation before reconcile reaches `committed` or `rolled_back`.
- Important: the current release manager records `services_stopped`,
  `integration_bundle_switched`, and `services_started` as transaction
  checkpoints but does not call `launchctl` itself. Verify the real service
  state independently.

## Failed cutover or release rollback

- Notice: doctor returns an error, the factory profile does not stay healthy,
  Linear freshness fails to recover, or the sandbox smoke does not execute
  through the expected release.
- Do: leave `MAINTENANCE` present and stop only the product's factory profile
  and reconciler. If the activation transaction is interrupted, run
  `factory-kit.sh reconcile` first and follow its terminal result.
- Do: merge the normal protected revert from a `chore/<slug>-revert` branch that restores both the previous full
  `KIT_PIN` and product tree, then update and verify the clean product checkout.
  If the candidate generation is committed and still active, run
  `factory-kit.sh rollback`; if reconcile restored the previous generation or
  the candidate never committed, do not call rollback.
- Do: prove the previous release can read candidate-written state, restart the
  factory-only services, rerun doctor and sandbox smoke, then remove
  maintenance.
- Target: restore known bits within 5 minutes and complete the full rollback
  within 30 minutes of the failed-health decision. Full rollback ends only
  after the pin revert is merged, previous tuple and state compatibility are
  verified, health passes, and maintenance is cleared.
- Don't: run rollback before the protected pin/tree revert, edit the pin
  locally, add a bypass, clear maintenance after pointer rollback alone, or
  claim the drill passed without timestamps and evidence.

### Relay generation-1 exception

Relay's legacy runtime had no previous `active.json`, so its 2026-07-15 drill
did not call `factory-kit rollback`. With candidate maintenance still
published, the operator stopped only the Relay candidate gateway/reconciler,
restored the hash-verified legacy profile, registry, and reconciler definition,
removed the legacy kill barrier, and proved the preserved Blocked-Escalated
T-106 state and Linear map were readable. The legacy proof was recorded at
`05:45:22Z`; candidate recutover doctor passed at `05:45:58Z`.

This exception is exhausted. For generation 2 onward, use the protected
pin/tree revert before the normal rollback command, recertification, and health
sequence above.
The first cutover's activation-to-clear interval was 5m50s, so the five-minute
outage target was missed and must not be reported as accepted.

## Release retention

- Notice: old exact-SHA release directories, receipts, and journals accumulate
  under `~/.factory/kits`.
- Do: retain active and previous generations, every nonterminal ticket lease,
  every receipt/journal reference, rollback-readable state versions, and
  several older certified releases. Manual deletion requires a minimum age,
  multiple successful real tickets, a successful rollback drill, state
  compatibility evidence, and a zero-reference audit.
- Don't: call `prune`; automatic pruning is intentionally not implemented. Do
  not delete a release merely because it is not currently active.

## Authoring epics and big tickets (operator-side tools, not factory stages)

These run in your interactive session — never inside the loop. The factory's own spec quality gate is the spec-linter stage; these tools raise the quality of what you feed it.

**spec-kit, pinned install.** `specify` v0.12.11 is installed via `uv tool install specify-cli --from git+https://github.com/github/spec-kit.git@v0.12.11`; the authoring workspace is `~/Projects/spec-authoring` (claude integration, `.specify/` + `.claude/commands/`). Flow for an epic: `/speckit-specify` (write the spec from an intent) → `/speckit-clarify` (structured de-risking questions) → `/speckit-checklist` (requirements-quality checklist) — then carve the result into factory tickets by hand, or use `/speckit-taskstoissues` to seed the board. Do not use `/speckit-implement`, `/speckit-plan`, or `/speckit-tasks` to produce factory artifacts — they duplicate the Planner/Builder and skip the factory's gates. Upgrading the pin: bump the `uv tool install` tag, then refresh `vendor/spec-kit/` per its README.

**gstack planning suite (already installed under `~/.claude/skills/gstack/`).** Interactive-only by its own design (its preamble blocks headless runs). Useful for instantiation-scale documents: `/office-hours` and `/plan-ceo-review` to pressure-test scope, `/plan-eng-review` for an engine spec, `/spec` for authoring a single rich ticket. Treat their output as draft input to the Planner, not as a frozen contract — the Planner still owns the ticket and the spec-linter still lints it.

## Linear initiatives and approvals

- Create the durable initiative record first at `factory/initiatives/I-NNN.md`; the reconciler creates the Linear Project. Set its status and target date in Linear.
- Assign an issue to a different initiative by changing its Linear Project. The next successful pull updates the ignored operator overlay; trusted materialization updates `Initiative:` on the ticket branch. Removing all Project membership clears the effective initiative and makes preflight ineligible until the issue is assigned again.
- Prioritize by setting priority and moving Backlog → Ready. Wait for sync health to advance before dispatching.
- Contract 1.2 stops in Review. Under contract 1.3, wait for trusted bundle attestation to create Awaiting Approval, then make the one business decision by moving it to Approved in Linear. Do not click a separate GitHub approval or bypass protection; the trusted approval attestation requests auto-merge. Done appears only after the protected closeout commit merges.
- Resume an escalated ticket by setting `Resume-State:` locally to the agreed stage, then move the Linear issue out of Blocked-Escalated to that same stage. Mismatched or otherwise illegal transitions are rejected and reported in sync health.

## The general rule

When unsure: kill switch first (it's always safe), read the ledger and the ticket trail second, escalate to a fresh planning session third. Nothing in the factory is made worse by stopping it.
