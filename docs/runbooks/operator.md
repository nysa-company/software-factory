# Factory operator runbook

What to do when something breaks, written for a non-technical operator. Each entry: how you notice, what to do, what not to do.

## Stuck ticket (no movement for hours)

Keep the supervised `watch --json` consumer running. For an authenticated
`progress_timeout`, inspect the named evidence and use only the recovery path
in that action; never relaunch a role or move Factory-owned state by hand.

## Watch for operator action

- Run `factory-launch <project> watch --json` in a supervised terminal or feed
  its newline-delimited JSON to your notification channel. It reports only an
  approval request, escalation or contract blocker, terminal role failure,
  terminal recovery refusal or abandonment, budget halt, Spec-linter round-three
  authorization action, or progress timeout; it does not send Slack or desktop
  messages itself. Intermediate recovery failures stay silent while the bounded
  recovery remains active.
- Save the opaque `cursor` from the last handled line. After a restart, pass it
  back as `--cursor <value>` so that line is authenticated and not delivered
  again. `--limit N` and `--idle-timeout-seconds N` support bounded drains.
- A line tagged `operator-watch-diagnostic/v1` with reason
  `factory_identity_unavailable` reports one authenticated historical action
  whose Factory SHA cannot be recovered. Inspect that source event; do not
  infer a SHA. Continue from its cursor so later actions remain visible.
- A nonzero exit means the selected controller stream, cursor event, ownership,
  mode, path, or digest was lost or changed. Stop the consumer and inspect the
  sealed lane; do not invent a cursor, skip files, or read another project's
  controller directory directly.
- The command is read-only and credential-free. Keep channel credentials in the
  external consumer, never in the Factory launcher, controller state, cursor,
  or event payload.
- If the controller stops after durably recording a blocked, budget,
  Awaiting Approval, or failed-role boundary but before publishing its event,
  the next reconcile reconstructs that one event from the exact authenticated
  claim, transition, passport, and terminal evidence. Do not create a manual
  substitute; restart with the last watcher cursor.
- There is no push notification (no Slack/email/desktop alert) — that is a
  deliberate decision, not a gap. `watch --json` is the hook for adding a
  notifier later; today it only feeds a supervised terminal or your own
  consumer. To check what needs you right now instead of glancing at a board,
  run
  `bash scripts/factory-kit.sh operator pending --project <project> --product <absolute-product-path>`.
  It lists tickets sitting in Awaiting Approval or Blocked-Escalated plus any
  open (issued but not yet consumed) operator receipts.

## Park a ticket on a Factory defect

- Open the defect in the Software Factory GitHub repository, then wait for the
  ticket to reach an idle passport boundary.
- Park only that ticket with
  `factory-launch <project> ticket-control pause --ticket T-NNN --issue <issue-url> --json`.
  The sealed controller releases its lease and retains an owner-only repro
  record; sibling tickets continue.
- After the successor Factory and any approved route migration are ready,
  resume deliberately with
  `factory-launch <project> ticket-control resume --ticket T-NNN --factory-sha <full-sha> --json`.
- A v2 pause at the pre-route head may resume through exactly one pushed,
  Factory-authored ticket-and-route migration child. The resume command binds
  that head while migrating the passport, revalidates the signed pause and
  authenticated lineage, then claims capacity. Arbitrary descendants, remote
  drift, extra paths, legacy v1 pauses, and changed lifecycle or run evidence
  still refuse.
- Do not move Markdown or operator-map state by hand. A missing issue, changed
  passport/state, active role, or different target Factory refuses cleanly.

## Runaway spend

- Notice: daily spend rollup jumps, or a provider console alert fires.
- Do: run `scripts/kill-switch.sh` immediately (safe — it stops, it doesn't break anything). Read `factory/runtime-ledger.csv` for today's effective rows and find the expensive role/ticket; tracked `factory/ledger.csv` is close-out history and may be stale during a live incident. That ticket goes to Blocked-Escalated; resume the rest by removing `factory/KILL`.
- Don't: rotate API keys for a spend problem — that's for leaks, and it kills your own sessions too.

## Envelope and cap control backend

`scripts/envelope-control.py inspect` returns permanent defaults and each role's
exact inherited attempt values. Permanent changes are two-step operations:
`plan --set KEY=value` returns a deterministic `preview_hash`; repeat the same
arguments with `apply --approve-hash <hash>`. Apply takes the product launch
lock, compare-and-swaps the exact `ENVELOPE.env` and `ENVELOPE.md` inputs, and
publishes each file atomically as a consistent pair (rolling back a failed
second publication). It rejects symlinks, hard links, foreign
ownership, writable-by-others paths, malformed limits, and stale previews.

For a bounded exception, use `override-plan` and `override-apply` with one of
`next-attempt`, `ticket`, `role`, `product-day`, or `global-day`. Ticket and
role selectors narrow the record; day scopes require a UTC `--day YYYY-MM-DD`.
Every override binds an operator ID, reason, issue time, and expiry of no more
than seven days; preview/apply approval expires after 15 minutes. Overrides are
immutable JSON records. A next-attempt use writes a separate consumption
receipt, so neither the authorization nor prior accounting is ever rewritten.
Expired records are ignored and conflicting active records fail closed.
`global-day` additionally
requires `--global-env /absolute/path/to/global.env` and stores its record beside
that machine configuration so every product using it observes the same cap.

The sealed launcher must expose these commands explicitly before operators use
them remotely. Direct backend examples for launcher integration:

```bash
python3 scripts/envelope-control.py inspect --factory-root /absolute/product
python3 scripts/envelope-control.py plan --factory-root /absolute/product --set BUILDER_PER_RUN_BUDGET_USD=8.00
python3 scripts/envelope-control.py apply --factory-root /absolute/product --set BUILDER_PER_RUN_BUDGET_USD=8.00 --approve-hash <preview-hash>
python3 scripts/envelope-control.py override-plan --factory-root /absolute/product --scope next-attempt --ticket T-123 --role builder --issued-at 2026-07-19T01:00:00Z --expires-at 2026-07-19T01:15:00Z --operator-id operator-1 --reason budget_exhausted --set BUILDER_PER_RUN_TIMEOUT_MIN=60
```

## Local control console

Run `python3 scripts/operator-console.py`, then open the one-use loopback URL.
The project selector lists only validated owner-controlled `active.json`
records. Workflow,
model candidates, effective envelope values, and daily spend are read through
fixed Contract 1.5 launcher commands.

Every mutation is two-step. Preview the exact policy, envelope, override, or
cancellation; inspect its JSON; then apply that preview hash. If an active
attempt needs more budget, preview and apply cancellation first. Pre-GO
cancellation costs zero; post-GO cancellation retains the conservative
reservation. Apply the bounded override and restart the same role. Never edit
an in-flight manifest or backdate an override.

## Failed deploy / broken staging

- Notice: staging URL errors, or a Narrator bundle says "preview broken".
- Do: check Railway's dashboard for the failing deploy log; the usual fix is reverting the last merged PR per `docs/operations/rollback-drill.md`. If staging is down but no recent merge happened, restart the Railway service from its dashboard. When the Factory has already emitted `preview-identity-timeout` and the same pushed head should be checked again, run `factory-launch <project> ticket-control retry-preview --ticket <T-NNN> --operator-id <ID> --json`; it extends only that exact wait and does not waive preview evidence.
- Don't: approve anything while staging is broken — bundles can't be verified.

## Leaked secret (a key appears in a file, log, or commit)

- Notice: a scanner alert, a reviewer comment, or you see a key string somewhere it shouldn't be.
- Do: this is the one case for rotation. Revoke the exposed key in the provider console, issue a new one, update GitHub/Railway secrets. If it reached git history, treat the repo history as public: rotate everything that repo ever saw. Then file a ticket to fix the path it leaked through.
- Don't: just delete the file and move on — the key is still burned.

## Model provider down (Claude or OpenAI outage)

- Notice: runs fail immediately with API errors; provider status page confirms.
- Do: if the primary is unavailable before submission, let the pinned route probe refuse the run. After a terminal, fully accounted provider failure, use the sealed `models fallback-plan` flow below. If agents are scheduled, `scripts/kill-switch.sh` remains the safe stop.
- Don't: manually swap families or relaunch. The fallback transaction preserves partial work, excludes the failed route, and rechecks contributor-family boundaries.

## Production provider concurrency is not ready

- Notice: Doctor, certification, activation, or a Contract 1.8 role refuses
  because provider concurrency is absent, incomplete, or drifted.
- Do: keep maintenance published and drain roles, leases, provider attempts,
  and legacy intervals. Use the installed sealed release's
  `scripts/factory-kit.sh provider-concurrency plan` command for that Factory
  SHA and the product capacity, review its exact routes, then apply only the
  returned approval hash. Re-run `check` and Doctor before certification.
- Don't: handwrite `~/.factory/provider-policy.json`, copy qualification
  configuration into production, reduce the ticket capacity to bypass the
  gate, or replace the owner-local runtime directory after certification.

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

## A semantic loop reached its next-round authorization boundary

- Notice: after two Spec-linter FAIL verdicts, and before every later Planner–Spec-linter round, `next-stage` returns provider-free `AWAIT-OPERATOR` with the exact next `OPERATOR AUTHORIZATION: spec-linter round <N>` line. Contract repair does the same before its fourth and every later `FIX <role>` attempt. After the one automatic Narrator bundle correction, every later invalid bundle waits for the exact next Narrator round. A duplicate or malformed attempt produces a typed correction action without launching a provider.
- Do: if one more cycle is warranted, use the exact role and round from the watcher: `factory-launch <project> ticket-control authorize-round plan --ticket <T-NNN> --role <role> --round <N> --operator-id <ID> --json`, review its identities, then run the matching `authorize-round apply` with `--approve-hash <HASH>`. The controller writes and pushes one ticket-only child; ordinary reconciliation imports it. Each line grants only that next round.
- Don't: add commentary to the authorization line, authorize a different role or round, change another path, or infer authorization. Reviewer remains governed by its budget-only review loop and has no semantic-round authorization gate.

## One successful Reviewer run is a duplicate

- Notice: reconciliation refuses because authenticated successful Reviewer runs exceed recorded verdicts by exactly one. The evidence proves one duplicate exists but cannot identify which run because verdict lines do not bind run IDs.
- Do: choose the duplicate ledger ordinal and run `factory-launch <project> ticket-control reviewer-void plan --ticket <T-NNN> --run <N> --operator-id <ID> --json`. Review the selected run identities and approval hash, then run the matching `reviewer-void apply` command with `--approve-hash <HASH>`. The controller pushes one ticket-only child containing exactly `OPERATOR NOTE: reviewer run N void — duplicate`; ordinary reconciliation imports it without launching a provider.
- Don't: infer the ordinal, edit the parked cell, add a generic operator note, void a failed or already-voided row, or reuse the command when zero or multiple successful rows are unmatched. Those cases require new evidence or a separate repair.

## GitHub or Railway down

- Do: `factory/operator-map.json` is a local projection with no external
  system behind it, so there is nothing to wait on for staleness — an
  operator action lands as soon as its receipt is issued. GitHub or Railway
  outages still pause the stages that depend on them.
- Don't: hand-edit the operator map or force local state to imitate an
  operator transition that has no matching receipt.
- Note: on a new product, the first normal reconciliation may durably
  initialize the missing real `factory/runs/` directory. A file, symlink, or
  other invalid entry at that path is an integrity failure, not something
  sync replaces.

## Broken connector (external sends failing)

- Notice: tickets with the `external` label fail their sends; receipts/error comments show it.
- Do: confirm the connector's sandbox/production mode and its credentials in the product's settings. Flip nothing to production while debugging. Escalated failures are a ticket for the factory, not a manual workaround.

## Restore from backup

- Postgres (staging): Railway dashboard → database → Backups → restore. Staging data is disposable; fixtures re-seed it.
- Board: restore the product repo first. Markdown and the ledger are the durable execution record. `factory/operator-map.json` is a gitignored local projection with no external system behind it — delete it and let the next reconciliation regenerate it from ticket files and the consumed receipts under `factory/receipts/`; nothing durable is lost.

## Model portfolio control

The operator owns profile activation and temporary credit-exhaustion overrides.
The complete primary/secondary table and enforced fallback rules are in
[model-routing.md](../model-routing.md).
Use only the selected release through the sealed launcher:

```bash
~/.factory/bin/factory-launch <project> models profiles --json
~/.factory/bin/factory-launch <project> models inventory --json
~/.factory/bin/factory-launch <project> models plan --profile openai-priority-v1 --json
~/.factory/bin/factory-launch <project> models activate --profile openai-priority-v1 --approve-hash <preview-profile-hash> --approved-by <operator-id> --json
~/.factory/bin/factory-launch <project> models disable --scope-type account-route --scope-id codex-native --reason credits_exhausted --ttl-seconds 3600 --operator-id <operator-id> --json
~/.factory/bin/factory-launch <project> models enable --scope-type account-route --scope-id codex-native --json
~/.factory/bin/factory-launch <project> models pin --ticket T-123 --workdir /absolute/ticket-worktree --json
~/.factory/bin/factory-launch <project> models migrate-plan --ticket T-123 --workdir /absolute/ticket-worktree --json
~/.factory/bin/factory-launch <project> models migrate --ticket T-123 --workdir /absolute/ticket-worktree --approve-hash <preview-hash> --readiness-hash <preview-readiness-hash> --approved-by <operator-id> --json
~/.factory/bin/factory-launch <project> models fallback-plan --ticket T-123 --failed-run <run-id> --workdir /absolute/ticket-worktree --reason credits_exhausted --json
~/.factory/bin/factory-launch <project> models fallback --ticket T-123 --failed-run <run-id> --workdir /absolute/ticket-worktree --reason credits_exhausted --json
```

Use the sealed `models inventory` command for Cursor model discovery. It first
requires the exact configured CLI identity in an empty disposable owner-only
home, then copies owner-only credentials into that home, removes the copy on
exit, and never writes the source `~/.cursor` files. Owner-only capture files
are accepted only after stable bounded reads. The command returns exact
selection IDs from the certified `Available models` response; headers, display
labels, current/default markers, and the usage footer are presentation only. An
unknown response shape fails closed—do not parse it manually against the ambient
account. Do not run ambient `agent models` or `agent status` while another lane
is active because
those commands may rewrite shared Cursor configuration permissions.

`models plan --json` previews the active profile, or default
`cursor-opus-v1` when none is active. Activation accepts only the exact
profile hash shown by preview. `openai-priority-v1` tries OpenAI production
first, then Anthropic production; `claude-priority-v1` reverses those
portfolios. `cursor-priority-v1` tries exact Cursor OpenAI/Anthropic routes
before native routes in both portfolio orders with the older effort policy.
`balanced-v2` retains the high-effort native-first portfolio. Legacy has one
native-first OpenAI-production/Anthropic-checking portfolio.

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

Contract 1.4 tickets migrate the v1 plan once with `migrate-plan`, followed by
`models migrate` using the exact preview and readiness hashes plus operator ID.
The ordinary preview is compact; add `--include-journal` before `--json` only
when the complete authenticated candidate is needed for diagnostics. For a later
eligible failure:

1. Run `fallback-plan` and note its exact `preview_hash`.
2. Run
   `bash scripts/factory-kit.sh operator fallback-approve --project <project> --product <absolute-product-path> --ticket T-123 --preview-hash <preview_hash> --failed-run <run-id> --reason credits_exhausted`
   (`--reason` also accepts `budget_exhausted`, `operator_requested`, or
   `provider_unavailable`; `--expires-minutes` defaults to 60). This issues a
   one-use operator receipt, projects the `model_fallback_approval` decision
   into `factory/operator-map.json`, and writes the zero-authority audit copy
   under `factory/receipts/T-123/`.
3. Run the same `models` command with `fallback` instead of `fallback-plan`;
   it consumes the receipt by its `receipt_sha256`.

The apply step refuses active provider/accounting locks, Git or evidence drift,
unapproved paths, protected ticket-field changes, secrets, and unsafe files. It
commits the validated partial snapshot and the next append-only route-journal
revision together, pushes the exact branch head, then consumes the approval.
Only `credits_exhausted` and `provider_unavailable` are eligible reasons.

For qualification, run `doctor --json` before starting reconciliation and
inspect `checks.fallback_readiness`. A version mismatch names the exact native
fallback route plus expected and installed versions. Admission also requires
the fresh report digest to match the sealed qualification receipt. Do not replay a completed
Cursor role or edit its claim/passport: exact identity-only success is recovered
without another provider call, while a typed `qualification-fallback-refused`
claim remains parked until a successor release contains the repair.

Kimi K2.6 remains disabled experimental through Claude CLI/OpenRouter/Moonshot.
No live or billed pilot has run. Rotate the credential before a pilot; direct
same-UID token exposure remains until a broker or OS isolation is used.

## Certification operator preflight

Before maintenance or certification, run the candidate Factory's read-only
report against the exact intended cohort:

```bash
bash scripts/factory-kit.sh preflight-report \
  --project <project> --product <absolute-product-path> --sha <full-kit-sha> \
  [--ticket <T-NNN>] --json
```

The closed JSON report verifies the manifest-backed candidate identity, clean
product `main` at the exact read-only `ls-remote` result from the validated push
authority, matching identity snapshots around all evidence reads, candidate
pin, current Node/npm PATH tuple, certification network declaration, each
selected ticket's existing readiness contract, pairwise Builder ownership,
and every committed terminal ticket needed by activation. It hydrates only
the immutable `refs/pull/<number>/head` objects named by committed migration
evidence, without moving local refs or `FETCH_HEAD`, and reports all terminal
blockers together. Every
accepted invocation emits exactly one report: `pass` exits 0,
`authorization-required` exits 3, and `blocked` exits 2. A required network
phase is not auto-approved: review the named phases, then explicitly rerun the
same command and later certification with
`FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1` only when warranted.

The report does not run Doctor or model/provider probes, write owner state,
publish maintenance, certify, activate, or make the operator decision. Use the
existing `status --json`, Doctor, and provider controls for active runtime and
transaction diagnosis.

## Preflight failed before launch

- Notice: the dispatcher escalates with `PREFLIGHT FAIL` output from the launcher's `preflight` route — no pinned safe route, adapter contract/version mismatch, budget headroom, git state, or ticket not Ready.
- Do: read each FAIL line. Common fixes: inspect `models status --json` and `models plan --json`; run `scripts/adapters/contract-test.sh --routes`; reconcile pinned CLI versions in `~/.factory/global.env`; authenticate the affected account route without printing credentials; raise `DAILY_CAP_USD` or `GLOBAL_DAILY_CAP_USD` if the projected reserve no longer fits; clean and sync the repo to `main`; confirm the ticket is Ready. Re-run preflight through `~/.factory/bin/factory-launch <project> preflight` before resuming.
- If the blocked ticket already has a passport, make one direct ticket-only
  commit that changes only `Product-Decisions`, `Builder ownership`,
  `Fixture-Seams`, or `Authentication-Seams`, and append the exact
  `OPERATOR PREFLIGHT RECEIPT: <receipt>` and
  `OPERATOR PREFLIGHT FAILURE EVENT: <event_sha256>` lines reported by the
  controller. Push that exact head; ordinary reconciliation validates and
  retries it without another provider run.
- Don't: tell the dispatcher to launch anyway — every FAIL is predictable at kickoff and will block mid-pipeline.

## Trusted approval and close-out PR

- Notice: under contract 1.3+, after the bundle is attested and Awaiting Approval, make the one business decision by reviewing the exact bundle yourself and running
  `bash scripts/factory-kit.sh operator approve --project <project> --product <absolute-product-path> --ticket T-123`.
  This requires a machine with the controller state dir (`$PROJECTS_DIR/<project>/controller`) — approvals cannot be issued from an arbitrary checkout. The command binds the one-use operator receipt to the attested bundle blob and requests protected GitHub auto-merge. If it refuses stale evidence, a changed head, conflicts, unavailable auto-merge, or failed checks, investigate the named condition; never manually imitate the attestation.
- Notice: if main advances, use sealed `ticket-attest --action refresh` on the exact ticket worktree. It disables stale auto-merge, merges protected main without force, retires the old receipts, and makes fresh Reviewer, Narrator, bundle, and operator approval evidence mandatory.
- Notice: after the ticket PR merges, the dispatcher opens `chore/tNNN-closeout` from current `origin/main` and invokes `ticket-attest --action done`. It verifies the merge and configured post-merge contexts, projects the ledger once, commits Done plus closeout evidence, creates or reuses the exact factory-owned closeout PR, and requests protected auto-merge. No operator approval or manual GitHub merge is required. After protected main contains valid Done, sequencing returns `COMPLETE` and the dispatcher releases the lease. Projection refuses any active or ambiguous claim.
- Note: Done closeout no longer has an external witness. Under the removed
  Linear integration, the projected board move to Done was a second,
  independently observable record; now closeout is Git- and receipt-only. This
  is an accepted trade-off of the removal, not an oversight — verify Done the
  same way you verify anything else here, by reading protected `main` and the
  ledger, not by checking a board.
- Emergency only: for an already-merged ticket that cannot satisfy normal approval evidence, create one owner-only request naming an open GitHub issue, operator, 20+ character reason, and a current `issued_at`/`expires_at` window of at most 24 hours. Run sealed `ticket-attest --action emergency-plan --request <absolute.json>` from the clean `chore/tNNN-closeout` worktree, review its exact PR/check/main/passport basis, then run `emergency-apply` with the returned `approval_sha256`. Apply reuses the ordinary ledger projection and protected closeout PR; it never fabricates bundle or approval evidence. An authenticated target may use either its exact idle blocked claim or a controller-signed pause bound to the same ticket, head, state, passport, and open Factory issue. A passportless target is accepted only when protected main explicitly identifies it as `Assignee: operator (built outside the software factory)` and no runtime claim, passport, or pause exists. Every use needs its own issue and exact plan-hash approval.
- Emergency role admission only: stop the controller at an idle exact claim, then create an owner-only `nysa.software-factory.emergency-role-admission-request/v1` naming one open Factory issue, non-`auto` operator, 20–500 character reason, and current expiry of at most 24 hours. Run sealed `emergency-admit plan` with the exact ticket, role, receipt, worktree, and request; review every bound identity and approve only its returned SHA-256. Run `emergency-admit apply` with the same inputs and that hash, then restart ordinary reconciliation. The launcher uses this authorization only if normal receipt consumption rejects and only before provider submission. Confirm one `emergency_admission_archived` event names the resulting run and passport charge. A changed, consumed, or capped receipt, claim, head, route, passport, lease, issue, maintenance state, active run, publication authority, or plan hash must be investigated rather than overridden.
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
- Do: use `legacy-reviewed` only for exact Review. T-013 through T-016 alone
  use `legacy-reviewed-aggregate` because their PRs predate separate
  `policy`/`app-tests` jobs; require their authentic app-bound aggregate `ci`
  and `test-immutability` checks plus fresh independent-audit and combined-test
  digests. T-019/T-020 are the only permitted `out-of-band-merged` Planning
  anomalies and require the same audit digests. A historical ticket has no
  route plan and the legacy receipt must say so.
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

## One-time pre-contract terminal backfill

- Notice: this is a second, independent bounded migration for exactly the
  authorized pre-contract terminal-Done batch. It neither extends
  `factory/migrations/contract-1.3/` nor permits arbitrary historical Done.
- Do: generate from an exact clean protected-main basis with
  `scripts/terminal-backfill.py --product <repo> --request <reviewed-request>`.
  Review the exact repository, T-001–T-012 batch, target kit, cutoff, immutable
  source ticket blobs, implementation and closeout PR ancestry, ledger rows,
  and each implementation era's authentic app-owned successful checks.
- Do: preserve absent historical evidence as null. A missing source bundle,
  Kit-SHA, or route plan must never be reconstructed. Every source ticket must
  already be Done.
- Do: put `factory/migrations/contract-1.3-terminal-backfill/` and the target
  `KIT_PIN` in one manual protected product PR. Disable auto-merge and bypass.
  The evidence is inert until that exact protected merge.
- Don't: hand-edit generated evidence, accept partial or extra receipt files,
  reuse the authorization for another repository/basis/cutoff/kit, or invent
  checks and approvals. Conflicts with normal attestations or the first legacy
  batch fail closed.

## One-time protected-merge reconciliation

- Notice: this is a migration-only adoption of an exact authorized batch whose
  product changes already reached protected main but whose older factory
  evidence cannot be refreshed safely. It is not ordinary in-flight migration,
  normal Done closeout, or a reusable compatibility mode.
- Do: generate from a reviewed request and clean protected-main basis with
  `scripts/protected-merge-reconciliation.py --product <repo> --request
  <reviewed-request.json>`. Review the authorization, exact receipt set,
  immutable per-ticket `evidence_head`, source/review/merge/check evidence,
  current product blobs, basis and target kits, and fresh operator adoption.
- Do: commit the complete authorization, receipts, Done/Migration ticket
  projections, target `KIT_PIN`, and any explicitly authorized companion
  path/blob entries together. Disable auto-merge and bypass; only the manual
  protected merge of that exact product head grants authority.
- Don't: migrate or repin the reconciled tickets, synthesize missing approval,
  accept a partial/extra batch or an unbound companion, hand-edit generated
  evidence, or use the format for later tickets. The reconciliation directory
  and terminal projections remain immutable after adoption. The target pin and
  companion blobs are bound at that protected introduction but may evolve in
  later authorized releases without rewriting the historical batch.

## Test commit order before operator review

- Notice: a newly frozen numbered contract may legitimately reopen
  Test-author ownership after implementation under the superseded contract.
- Do: update the instantiated product gate from the certified Factory template
  and verify the ticket-only contract-freeze commit starts one new epoch. New
  Planner output appends the canonical PASS marker. Historical output may
  replace only the latest heading and matching established PASS marker
  one-for-one; do not reorder across Planner, Test-author, Builder, or Reviewer
  evidence.
- Notice: within one unchanged contract epoch, reviewer-requested test commits
  after implementation still fail the test-immutability gate.
- Do: ensure `scripts/reorder-test-fixes.sh` is present. Use it only on a clean
  local same-contract tail and only when its final tree is byte-identical.
  The helper refuses to move a commit across a merge. Retained two-parent
  merges keep their exact reviewed tree and protected second parent; octopus
  merges are refused. The helper is never force-push authority.
- Notice: an already accepted late Test-author push is a protected recovery,
  not an ordinary local reorder. First activate the successor Factory and let
  it migrate the signed passport on the unchanged old ticket head. Run the
  helper locally from the exact protected merge base, but do not push yet.
- Do: verify the old/new heads, identical trees, passing immutability gate,
  unchanged protected merge parents, route digest, and exact accepted
  Test-author Factory/run/receipt. Commit only the canonical
  `factory/migrations/ticket-rewrite/<new-head>.json` authorization directly
  above that protected base and merge it through protected CI. Then publish
  exactly once with `git push
  --force-with-lease=refs/heads/ticket/<ticket>:<old-head> origin
  <new-head>:refs/heads/ticket/<ticket>`. The controller will migrate evidence
  and resume only after local and remote ticket heads match exactly.
- Don't: publish before protected authorization, use a bare `--force`, edit or
  waive passport evidence, change the route, or retry a stale lease. Any drift
  requires a new reviewed authorization rather than modifying the old record.
- Notice: a successful Test-author may split one mixed test/implementation
  commit, append its required ticket log, and then fail the ordinary push
  because the remote history is intentionally replaced. Protected main may
  also have advanced independently since the ticket's replay base.
- Do: use one `ticket-history-repair-authorization/v1` record keyed by the
  exact new head. Bind the current passport and recovery Factory, the failed
  Test-author run/receipt and its historical `failed_test_factory_sha`, issue,
  operator, old remote head, route, 24-hour-or-shorter window, distinct
  `authorization_parent` and `replay_base`, and the exact force-with-lease
  target. The historical Factory must already belong to the authenticated
  passport release history and match the preserved receipt and manifest. Merge
  only that record through protected CI, then publish the exact new head once
  with the record's force-with-lease value. Let the sealed controller
  migrate/export the passport and resume Test-author normally.
- Don't: reuse the late-test normalization schema for a mixed-commit split,
  make protected main masquerade as the replay base, omit the append-only role
  log, hand-edit a passport, or promote the failed push to successful evidence.
- Don't: waive the immutability gate or ask the builder to edit tests post-implementation.

## Parallel kit work while production is running

- Notice: several kit features are being developed while a product remains on
  an older active release. This is expected; branches and merged candidate SHAs
  are inert.
- Do: give each authorized issue its own short-lived branch, linked worktree,
  focused regression, managed-readiness result, and draft PR. Keep the exact
  issue, source branch, and independently green commit in its review record.
- Do: when several independent repairs are required before the next sealed
  release, create one successor branch from the exact protected base and
  cherry-pick only those green commits. Preserve the issue-to-source-commit-to-
  candidate-commit map, run the combined focused regressions and managed
  readiness, and merge one protected successor PR. The exact merged tree must
  then pass the complete protected-main suite before one installation and
  sealed certification/qualification cycle. Canary compatibility-sensitive
  candidates.
- Do: serialize every product `KIT_PIN` change, activation, and rollback. Begin
  only at a ticket boundary with no active run, no conflicting nonterminal
  lease, no maintenance anomaly, and no incomplete activation journal.
- Do: keep contracts 1.0 through 1.5 at their default of one live ticket.
  Contract 1.6 defaults to four; contracts 1.1 through 1.5 permit an explicit
  capacity up to four, and Contract 1.6 permits up to six. One dispatcher holds no more than that many
  matching leases. Contract 1.8 caps capacity at four. Above one, its
  approval-hash-bound owner-local configuration must cover Cursor, Claude Code,
  and Codex at that capacity before certification; doctor and role admission
  then verify the same state. Older contracts, capacity one, and other legacy
  routes retain the product-wide provider lock.
- Don't: pull kit `main` into Sofia's live runtime, run from a mutable checkout,
  include a commit that is not independently green, or combine changes that
  overlap textually or semantically, share an unresolved trust boundary,
  depend on cherry-pick order, or change result when composed. Keep those
  candidates separate; never hide a failed gate in the batch, make an
  unreviewed conflict resolution, or overlap two activation/rollback
  operations.

## Upgrading the kit when multiple products run on it

- Notice: kit `main` moved while products remain on older immutable releases.
  This is normal. A merge is a candidate, not a deployment.
- Do: upgrade one product at a time. Install the exact merged SHA, update that
  product's full `factory/KIT_PIN` through its protected PR, certify the exact
  kit/product tuple, run sealed qualification and the native controller smoke,
  enter maintenance, drain, plan,
  and activate. Follow [../factory-runtime.md](../factory-runtime.md).
- Do: keep every other product on its own active generation. The stable
  launcher resolves releases per project.
- Don't: run live work from the mutable kit clone, replace `factory-launch` as
  part of an ordinary activation, delete or abbreviate the pin, patch an
  installed release, or upgrade all products with one untested pin change.

## Release doctor reports warning or error

- Notice: `~/.factory/bin/factory-launch <project> doctor --json` returns a
  warning/error status, a pin mismatch, maintenance, a
  lock, active/stale run records, a failed installed production controller, or
  unsupported Factory/CLI information. On Contract 2.0 macOS production, read
  `checks.controller.state`: `running` and `idle_clean` are healthy;
  `disabled`, `unavailable`, `route_mismatch`, and `last_exit_nonzero` are
  errors.
- Do: treat `error` as a dispatch stop. A warning requires operator review,
  not automatic repair. Confirm the selected full SHA, physical release,
  `KIT_PIN`, maintenance state, run PIDs, and CLI versions.
  For a controller error, inspect the managed LaunchAgent, native disabled
  override, and local logs; correct the supported installed route or
  underlying launch failure, then let the next scheduled reconciliation
  succeed and rerun Doctor. Qualification,
  disposable lanes, non-macOS hosts, and older contracts report the controller
  check as `not_applicable`.
  Doctor ignores only the three documented exact legacy resolution shapes in
  retained event history; those records cannot clear a current incident.
  Preserve the append-only history and investigate any other malformed or
  incident-bearing null identity as an error.
  Credential results are presence-only; test authentication separately
  without printing values.
- Don't: expect doctor to repair, authenticate, clear locks, kill processes, or
  rewrite configuration. It is deliberately read-only. Do not interpret an
  empty watcher result as proof that the controller is alive.

## Preparing and activating a release

For Contract 2.0, prefer the bounded two-command transaction below. The
numbered manual procedure remains the recovery/reference path for older
contracts and unusual host migration work.

```bash
bash scripts/factory-kit.sh release setup \
  --project <project> --product <absolute-product-path> \
  --repo <absolute-clean-factory-checkout> --sha <candidate> \
  --profile <profile-id> --operator-id <operator-id> \
  [--runtime-bin <absolute-node-bin>] \
  [--claude-bin <absolute-claude> --codex-bin <absolute-codex> \
   --cursor-bin <absolute-agent>] \
  [--ticket-workdir T-NNN <absolute-worktree> ...]

bash scripts/factory-kit.sh release resume \
  --project <project> --sha <candidate> --approved-by <operator-id>
```

`release setup` requires clean exact Factory and product Git trees, an exact
product `KIT_PIN`, Contract 2.0, and a reviewed certification-plan runtime. It
installs the sealed candidate, prepares the project-local Node/npm runtime,
runs the full activation preflight above through that runtime, then prepares
the ignored physical `factory/runs/` and `.active-runs/` roots, the path-only
`active.json.product_path` authority, and the exact macOS controller plist, then binds
Factory/product SHA and tree, active generation, runtime binaries, provider
plans, model profile, receipt, and any one-to-four ticket migration previews
into an owner-only plan. It also binds every committed ticket blob and state,
then idempotently initializes their local operator-map entries behind the
dispatch barrier. It never chooses migration tickets or advances ticket state.
Activation blockers therefore stop before product runtime preparation,
certification, maintenance, host reservation, or approval generation.

`FACTORY_KIT_TEST_MODE=1` is repository-test evidence only. It requires an
explicit owner-only `FACTORY_RELEASE_TEST_HOME` outside the real account home
with its kits root, product checkout, and single local product push destination
contained beneath that root, plus a local canonical Factory origin. Production
receipt validation requires the canonical GitHub Factory origin and
protected-main GitHub Actions evidence; test receipts cannot be promoted into
an installed production lane. The sealed test launcher forces the mock adapter,
records `repository-test` in run evidence, refuses GitHub-mutating commands,
and makes Doctor validate only that local mock boundary. Its controller stops
after the first authenticated Planning claim; it does not pin a production
model route or launch a role. Do not copy provider or GitHub credentials into
the isolated home.

If the stable launcher or provider settings must change, setup first returns a
`prerequisites` plan. Setup authorizes the exact sealed transaction; ensure
every listed active factory is already in maintenance and drained, then run
resume. That resume applies only the embedded child hashes, certifies the
product, and returns the second
`activation` plan whose hash binds the fresh one-use certification receipt.
Run the same resume verb again; the current owner-only sealed plan remains the
internal replay and integrity authority. When prerequisites
already match, setup returns the activation plan directly, so only one resume
is needed.

Activation keeps a durable dispatch barrier while maintenance is removed,
activates the exact model hash, replays the approved migration batch, initializes
the bound ticket inventory, loads the bound controller job, requires Doctor to
pass, and removes the barrier last.
Any cutover failure republishes maintenance and leaves dispatch stopped. A
retry with the same hash resumes the signed journal; changed product, runtime,
launcher, active record, controller, receipt, model, or migration
evidence is refused. Measure the automation target from setup process start to
the first controller-observed `Planning` ticket, excluding only the human time
spent reviewing an emitted hash; the production objective is less than 15
minutes.

1. Use a clean kit checkout whose remote is the canonical
   `github.com/nysa-company/software-factory` identity. SSH host aliases are
   intentionally not trusted and `--origin` does not override a mismatched
   checkout. Confirm the candidate full SHA is the current `origin/main` and
   its exact authenticated push run has all four Linux groups, all four macOS
   groups, the three stable evidence aliases per platform, aggregate `ci`, and
   `test-immutability` successful.
2. Install that exact sealed candidate. Reuse only the protected-main evidence
   from step 1 and run the local sandboxed host smoke; never substitute a local
   complete factory suite.
3. Inventory every nonterminal ticket and its committed `Kit-SHA`. Finish it
   on its current release or prepare the applicable exact protected-main
   migration evidence. Do not migrate its pin or route journal yet.
4. For an active execution computer, run `factory-kit.sh pause`. It publishes
   managed maintenance before checking the drain. If it refuses on a stale
   lease, leave maintenance published, prove no role run is active, run
   `factory-kit.sh recover-lease` once for each named ticket, and retry
   `factory-kit.sh pause`; never delete a lease by hand. After pause succeeds,
   keep dispatch stopped and prove every remaining provider attempt and
   reservation is drained. Only then install the exact sealed `factory-launch`
   with the fail-fast, rollback-preserving native command block in
   `docs/factory-setup.md` before changing other user-scoped tools. For an
   inactive replacement computer, keep its
   dispatcher, reconciler, and LaunchAgent disabled and install the sealed
   launcher before certification. Before the protected merge in step 7, also
   publish maintenance on the old active host, pause and recover its stale
   leases through the same supported sequence, prove its controller and
   provider work are drained, and keep it stopped through cutover.
5. From current product `origin/main`, prepare one clean canonical product
   checkout and commit the candidate `KIT_PIN`, operator-approved envelope
   values, and complete migration evidence on one migration branch. The tree
   proposed for the product PR must be the exact tree certified below.
6. On the computer that will execute factory roles, verify the configured
   `CODEX_PINNED`, `CLAUDE_CODE_PINNED`, and `CURSOR_AGENT_VERSION` values,
   controlled physical CLI paths, and `scripts/adapters/contract-test.sh
   --routes`. While maintenance and the complete drain from step 4 remain in
   force, run the candidate's `provider-cli-pin plan`, review its exact
   SHA/tree allowlist and physical targets, apply only that approval hash, and
   require `provider-cli-pin check --sha <candidate>` to report ready. This
   pin transaction precedes certification; certification and activation both
   reject an absent, stale, or unlisted receipt. Update and verify the Nysa
   Agents plugin for both Codex and
   Claude, restart agent sessions, and plan the repository baseline before
   certification. A baseline diff is a separate product change, not migration
   drift.
7. After every required `KIT_PIN`, in-flight authorization, and migration
   control is committed, open and merge the protected product PR while
   maintenance and the controller/provider drain remain in force. Fetch
   canonical `origin/main`, require the merged commit and tracked tree to be
   exactly the intended final product tuple, and make the canonical product
   path a clean `HEAD == origin/main` checkout at that commit. Then verify Node
   22 and any product certification dependency, including the
   product's configured local PostgreSQL endpoint. For Contract 1.8 capacity
   above one, preview and apply the exact owner-local provider configuration
   only after maintenance and complete provider/lease drain, as described in
   `docs/factory-setup.md`. For an active-project upgrade, certify that exact
   protected-main SHA and tree from the clean canonical product path and record
   the receipt ID and expiry. For a new project with no `active.json`, leave
   certification to the resumable release transaction in step 14 so one signed
   trace covers install through activation.
   Any later product commit or protected-main tree drift invalidates that
   receipt; land the complete final tree and recertify instead of attempting
   activation with stale evidence.
8. Complete sealed qualification and the native controller macOS smoke with a
   separate sandbox product. Never copy production credentials, operator state,
   active records, ledger, or LaunchAgent.
9. Confirm no active runs and no unauthorized nonterminal ticket with a
   different `Kit-SHA`. Activation scans committed local, tracking, and live
   remote ticket sources; a Done claim also requires a valid normal attestation
   chain or protected-main legacy closeout.
10. Immediately before activation, fetch canonical protected main again and
    require its SHA and tracked tree to match the certification receipt
    exactly. Drift requires a new protected product PR and recertification.
11. At replacement-host cutover, re-confirm the old host remains in maintenance
    with its runs, leases, and provider work drained. Confirm the old dispatcher
    is stopped; if that cannot be proven, revoke its execution access before
    proceeding.
12. For an active-project upgrade, run `factory-kit.sh plan`. It must report
   `No files were changed.` A new project has no prior generation to plan.
13. Stop only the native product controller. On a new project keep it disabled.
14. Use the same bounded release transaction for an active-project upgrade or
   a new Contract 2.0 project:

   ```bash
   bash scripts/factory-kit.sh release setup --project <project> \
     --product <absolute-product-path> --sha <candidate> \
     --repo <absolute-clean-kit-checkout> --profile <model-profile> \
     --operator-id <operator-id>
   bash scripts/factory-kit.sh release resume --project <project> \
     --sha <candidate> --approved-by <operator-id>
   ```

   The transaction reuses the existing install, certify, pause, qualification,
   and activate gates; it does not bypass them. Its owner-only signed plan and
   journal resume interrupted phases and refuse changed inputs. Restart the
   native controller only after the checks below, then collect Doctor JSON,
   sandbox smoke, PID, and repeated health probes. Tests alone are not
   production closure evidence; bind these observations and timestamps to the
   exact protected SHA.
   Before the plan mutates a project, `release abort` with the same project,
   candidate, and setup operator ID restores captured maintenance. Abort is
   refused after any active record changes.
15. For an authorized Contract 2.0 in-flight cutover, keep maintenance while
   collecting one `models migrate-batch-plan` preview for one to four exact
   ticket/worktree pairs. Review its protected-main and per-ticket bindings,
   then remove maintenance and apply only the returned hash with
   `models migrate-batch`. The existing per-ticket migration remains the
   authority for every child; partial results are independently retriable from
   the signed batch journal. A larger cohort retains the individual
   `models migrate-plan`/approved `models migrate` procedure. Claim fresh leases
   afterward. Without in-flight tickets, remove maintenance only after every
   acceptance check passes.

If a named unmerged ticket already has a prior-kit bundle, complete its sealed,
operator-approved route migration before issuing or consuming a receipt for the
ordinary protected-base refresh. A new-kit `Kit-SHA` refusal is a
`route-migration-required` gate, never refresh authority. Once the route is
current, the receipt-bound refresh may retire the stale bundle and return the
ticket to Review. An authenticated ticket whose implementation is already
merged skips provider routing and resumes ordinary closeout. The sealed route
writer admits the retained bundle only when its `kit_sha` exactly equals the
source route document's top-level `kit_sha` and differs from the selected
target; any incoherent bundle remains a refusal.
If the retained receipt is instead a roleless maintenance refusal from the
source release, do not delete or rewrite it. After the protected in-flight
release and route migration is complete, remove maintenance and run one
ordinary reconcile. The controller admits only the exact authenticated
migration suffix and requires the state machine's durable current parent-linked
receipt before removing the prior-release exclusion; any mismatch stays
ticket-local and parked.
When a dependency completes after bundle or approval, do not delete evidence
manually. The exact dependency receipt invokes the publication refresh, which
drafts the PR, retires the stale bundle/approval, and returns the ticket to
Review for fresh Reviewer and Narrator evidence. If a prior-kit bundle-only
artifact has already reached protected main, only an explicitly approved
emergency plan v2 may bind and delete that exact blob in its protected closeout
PR. The plan requires a complete authenticated stale-to-current release
lineage. If apply is interrupted, rerun the same hash-approved apply: its
owner-only signed journal restores only exact write-ahead ledger, ticket,
bundle, and Done bytes, and refuses any foreign edit. Current-kit or malformed
partial evidence still requires repair and must not be waived.
A controller restart after an exact publication refresh push uses the sealed,
lease-bound `dependency-refresh-replay` action. It authenticates the committed
refresh receipt and completes the post-push tail; it never reuses the consumed
transition receipt or creates a second merge or push.
A clean, correctly named, un-attested closeout retry is fast-forwarded to the
current protected main. Closeouts are serialized; a sibling waits while an
earlier exact Done closeout remains unmerged. If protected main advances and
leaves that clean, attested closeout PR behind, the attester closes the exact
stale PR and regenerates the receipt from current main with an exact
force-with-lease. Current-base bundles, dirty or divergent closeouts, invalid
passports, ambiguous PRs, and unproven merges remain blocked. A preserved
release-refresh marker remains reclaimable across later sealed upgrades.

Protected terminal validation treats pre-`run_id` ledger rows as exact legacy
row occurrences. Their order may change and new rows may be added, but every
attested occurrence must remain byte-for-byte present; modern rows continue to
require one unique non-empty `run_id`.

The run wrapper checks maintenance before taking the launch lock, after taking
it, and before GO. Never enable the replacement while the old host can still
dispatch.

For an explicitly approved in-flight cutover, first merge one protected product
PR containing the target `KIT_PIN` and
`factory/migrations/inflight-release/<target-kit-sha>.json`. The authorization
must name the product repository, old and target kit SHAs, and a sorted exact
list of Ready-or-later ticket branch heads and states. Each exact head must
still contain its old-kit v1 route plan or v2 route journal; activation
validates that the candidate's existing `models migrate` can consume it
without changing its logical routes or rewriting v2 history. Publish maintenance,
recover only the named
stale leases after proving there are no active runs, and leave zero lease
records before activation. After activation, keep maintenance while reviewing
each read-only `models migrate-plan` preview; remove maintenance before applying
each operator-approved `models migrate`. Existing v2 journals receive one
parent-hashed release-migration revision. Its probe-bound resolution refreshes
only adapter version and reported identity for the already-selected routes;
any route, family, effort, transport, account, or profile drift fails closed.
Legacy v1 plans retain their exact encoded provenance and receive the same
refreshed release-migration revision. Mutating migration refuses absent or
changed readiness evidence.
The one historical T-198 semantic-authorization recovery is different: its
exact protected-main in-flight entry is the migration approval, and the
controller consumes it through the same sealed plan/apply writer. Do not edit
its abandoned claim, passport, ticket, or route journal. A repaired release
must readmit the exact occurrence, push the authenticated two-path migration,
and append the second passport edge; every other in-flight ticket keeps the
manual preview and operator-approval procedure above.
It also revalidates the selected ticket against the same protected authorization
used by activation and refuses protected-main, remote-head, state, branch,
repository, or kit drift before writing. A retry is idempotent only for the
single direct migration child that changed that ticket's Kit-SHA and exact route
journal, with both paths still regular `100644` blobs; any other child requires
a new protected authorization.
Then claim fresh leases before resuming.
Any branch-head, state, source-kit, candidate-kit, repository, ticket-set, or
protected-main drift requires a new protected authorization. Never preserve or
copy an opaque lease into the authorization.

If a dispatcher lease is stale, keep maintenance published and run
`factory-kit.sh recover-lease --project <project> --product <path> --ticket
<T-NNN>`. Recovery refuses while any role run is recorded. Never delete or
replace the lease by hand.

If an idle parked ticket reports `state-machine-refusal` and protected main has
since changed, run ordinary reconcile before using ticket pause/resume. The
controller retries only when the refusal receipt (or its authenticated legacy
passport) binds the prior protected base and every ticket-local branch,
passport, route, run, and remote invariant is unchanged. An unchanged refusal,
dirty cell, remote drift, malformed evidence, active role, or pause stays
blocked. An interrupted retry is resumed from its ticket-local prepared marker,
exact dispatcher lease, and parent-linked child receipt; do not edit or remove
those records by hand. If input becomes unsafe before restart, reconcile retires
the marker and releases the lease, or records the lease on the blocked claim for
ordinary cleanup. Cancellation, completion, or another status change cannot
remove that claim until the marker-backed lease is accounted for.

The planned control-plane outage starts when maintenance is created and ends
when maintenance is removed. Target: 5 minutes or less. An inactive replacement
can prepare, certify, and canary before cutover; an active in-place host remains
in maintenance while its user-scoped tools are updated and certified. Relay
generation 1 did not establish this target; its activation-to-clear interval
alone was 5m50s and its full maintenance interval was longer.

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

- Notice: doctor returns an error, the native product controller does not stay healthy,
  or the sandbox smoke does not execute through the expected release.
- Do: leave `MAINTENANCE` present and boot out only the product's
  `com.factory.controller.<project>` LaunchAgent. If the activation transaction is interrupted, run
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

## Operator initiatives and approvals

- Create the durable initiative record first at `factory/initiatives/I-NNN.md` with its status and target date. There is no external board to create or sync a matching object in; set `Initiative: I-NNN` directly on the ticket.
- Reassign a ticket to a different initiative with a direct ticket-only commit that changes only `Initiative:`. This is Git-authored state, not an operator-map projection — there is no external system to round-trip through anymore. Clearing the field removes the effective initiative and makes preflight ineligible until the ticket is assigned again.
- Prioritize with
  `bash scripts/factory-kit.sh operator priority --project <project> --product <absolute-product-path> --ticket T-123 --priority <none|urgent|high|normal|low>`,
  then move it to Ready with
  `bash scripts/factory-kit.sh operator ready --project <project> --product <absolute-product-path> --ticket T-123`.
  Both take effect immediately once the receipt is issued — the operator map is a local projection with no external system, so there is no sync-health delay to wait on before dispatching.
- Contract 1.2 stops in Review. Under contract 1.3+, wait for trusted bundle attestation to create Awaiting Approval, then make the one business decision by running
  `bash scripts/factory-kit.sh operator approve --project <project> --product <absolute-product-path> --ticket T-123`
  from a machine with the controller state dir. Do not click a separate GitHub approval or bypass protection; the trusted approval attestation requests auto-merge. Done appears only after the protected closeout commit merges.
- Resume an escalated contract blocker in two pushed commits when an operator
  answer is needed. First append one ticket-local
  `OPERATOR ANSWER: <single-line-answer>` (at most 4096 UTF-8 bytes) and
  `OPERATOR ANSWER RECEIPT: <current-blocked-receipt-sha256>` pair, or replace
  the one prior pair for a later blocker. The answer is bounded non-contract
  context for the named repair role; it does not amend the frozen contract or
  authorize any other ticket field. That same commit may append one validated
  `Protected-Test-Conflicts` entry and, when needed, only its exact tracked test
  path already covered by protected `PROJECT.env` `TEST_PATHS` to
  `Fixture-Seams`. The complete ticket must pass readiness. Change no other
  ticket bytes or path. Push it, leave
  the ticket Blocked-Escalated, and wait for `contract_block_passport_migrated` when the
  controller records that event. Then make and push a ticket-only commit
  containing exactly `OPERATOR RESUME: <role>` and
  `OPERATOR RESUME RECEIPT: <current-blocked-receipt-sha256>`, replacing the one
  prior pair when present and changing nothing else. Do not combine the answer
  and resume pairs in one commit, and do not leave the resume commit local.
  On a later blocker, the old resume pair remains non-authoritative while only
  the new answer pair is present; the controller classifies that interval as
  waiting and preserves the claim, passport, role evidence, and accounting.
  If both commits are pushed before that migration event, recovery accepts only
  that one direct non-merge, receipt-bound context commit; every broader or
  longer chain stops as `resume_parent_not_migrated`. `factory/rulings.md`
  remains outside this exception.
  Finally run
  `bash scripts/factory-kit.sh operator resume --project <project> --product <absolute-product-path> --ticket T-123 --stage <Resume-State>`.
  A missing, stale, mismatched, partial, unpushed, over-full, or otherwise
  illegal decision is rejected; `doctor --json` reports it under
  `checks.contract_resume.incidents` with a typed reason. Doctor preserves the
  latest structurally valid `resume_*` refusal per ticket as a warning even
  when a newer controller introduces the reason; malformed or tampered event
  evidence remains an error. An earlier decision never authorizes a later
  blocker.
- Treat receipt-bound `OPERATOR RESUME` as the contract-block recovery and
  hash-approved `emergency-admit` as the one-use pre-provider control-plane
  fallback. Neither grants a lifecycle transition or skips any downstream
  gate. Envelope and semantic-round overrides grant no state authority. Do not
  edit state by hand or invent an environment or shell override; every
  emergency admission needs its own open Factory issue and exact plan-hash
  approval.

## The general rule

When unsure: kill switch first (it's always safe), read the ledger and the ticket trail second, escalate to a fresh planning session third. Nothing in the factory is made worse by stopping it.
