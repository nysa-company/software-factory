# Hermes integration and safe kit releases

Hermes reaches the software factory through one version-neutral command:
`~/.factory/bin/factory-launch`. The launcher resolves a product's active,
certified kit release once per invocation. A merge to the kit's `main` branch
does not change a running product.

This document is the reference and release procedure. The operator recovery
steps are in [runbooks/operator.md](runbooks/operator.md).

## Trust and storage model

The default state root is `~/.factory/kits`. Tests may override it with
`FACTORY_KITS_ROOT`.

```text
~/.factory/
├── bin/factory-launch
└── kits/
    ├── releases/<full-40-character-sha>/
    ├── manifests/<full-40-character-sha>.json
    ├── manifests/<full-40-character-sha>.suite.json
    ├── receipts/<receipt-id>.json
    └── projects/<project>/
        ├── active.json
        └── activation-journal/<generation>-<sha>.json
```

- `factory-launch` is the bootstrap trust root. Replace it only as an explicit
  contract migration, never as a side effect of activating a candidate.
- A release directory is materialized from one Git tree, contains no `.git`,
  rejects escaping symlinks, and is sealed read-only.
- `active.json`, not a convenience symlink, is the authoritative per-product
  selection. It binds generation, kit SHA/tree, receipt, product tree,
  contract version, product path, and physical release path.
- The launcher parses `active.json` once, validates the physical release path
  and tree, and then uses helpers only from that release for the invocation.
- External products must have `factory/KIT_PIN` containing exactly one
  lowercase, full 40-character SHA equal to the physical kit release.

The only implicit-pin exception is the repository's `conformance/` test bed
when it shares the kit repository, Git common directory, and HEAD. This
exception exists for in-kit regression tests. It is not valid for a live
product or deployment certification; external products and
`factory-kit.sh certify` require an explicit full `KIT_PIN`.

## Ticket release affinity

Before the first role, sealed `models pin` validates the clean exact ticket
worktree and certified origin, resolves all six routes, and records the
`Kit-SHA` plus `factory/route-plans/<T-NNN>.json` in one commit and exact-branch
push. Later preflight, sequencing, and role runs refuse a different physical
release. Roles select only their tuple from that plan and never re-resolve;
each run re-probes only that exact route. Blocked and resumed tickets retain
the same affinity.

Activation does not migrate ticket leases. Before activation, the operator
must verify that no nonterminal ticket is leased to a different SHA. The
current release manager rejects live run records but does not scan non-running
ticket state, so this operator check is required.

Run manifests record the kit SHA/tree, product tree, durable ticket `Kit-SHA`,
contract version, and physical kit path. The opaque dispatcher lease ID is a
capability available only to trusted launcher and kit helpers: it never enters
task text, model prompts, adapter environments, manifests, model output, or
public artifacts. The activated receipt ID is available from `active.json`;
the current run-manifest format does not copy that ID into each manifest.

## Public Hermes contract

Contract versions `1.0.0` through `1.6.0` certify Hermes Agent `0.18.2`, build
`2026.7.7.2`. The canonical manifest is
`integrations/hermes/contract.json`.

```bash
~/.factory/bin/factory-launch <project> contract --json
~/.factory/bin/factory-launch <project> doctor --json
~/.factory/bin/factory-launch <project> dispatch-plan --shadow --json
~/.factory/bin/factory-launch <project> dispatch-plan --claim --json
~/.factory/bin/factory-launch <project> ticket-pr --ticket T-123 [--lease <opaque-lease-id>] --workdir /absolute/ticket-worktree --json
~/.factory/bin/factory-launch <project> next-stage --ticket T-123 --workdir /absolute/ticket-worktree --json
~/.factory/bin/factory-launch <project> preflight --ticket T-123 --role planner --workdir /absolute/ticket-worktree --json
~/.factory/bin/factory-launch <project> ticket-state --ticket T-123 --workdir /absolute/ticket-worktree --action materialize --json
~/.factory/bin/factory-launch <project> ticket-attest --ticket T-123 [--lease <opaque-lease-id>] --workdir /absolute/ticket-worktree --action bundle --json
~/.factory/bin/factory-launch <project> project-ledger --ticket T-123 --workdir /absolute/chore-worktree --json
```

Under Contracts 1.5 and 1.6, pass the exact role returned by `next-stage` to `preflight`;
the launcher rejects roleless preflight so its envelope cannot differ from the
one reserved by `run`.

Model policy is task-free and sealed:

```bash
~/.factory/bin/factory-launch <project> models profiles --json
~/.factory/bin/factory-launch <project> models plan --json
~/.factory/bin/factory-launch <project> models plan --profile claude-priority-v1 --json
~/.factory/bin/factory-launch <project> models activate --profile claude-priority-v1 --approve-hash <profile-hash-from-preview> --approved-by <operator-id> --json
~/.factory/bin/factory-launch <project> models disable --scope-type route --scope-id codex-gpt-5.6-sol --reason credits_exhausted --ttl-seconds 3600 --operator-id <operator-id> --json
~/.factory/bin/factory-launch <project> models enable --scope-type route --scope-id codex-gpt-5.6-sol --json
~/.factory/bin/factory-launch <project> models pin --ticket T-123 --workdir /absolute/ticket-worktree --json
~/.factory/bin/factory-launch <project> models migrate-plan --ticket T-123 --workdir /absolute/ticket-worktree --json
~/.factory/bin/factory-launch <project> models fallback-plan --ticket T-123 --failed-run <run-id> --workdir /absolute/ticket-worktree --reason credits_exhausted --json
```

The operator activates only the exact profile hash returned by preview.
`cursor-balanced-v2` is used when no active record exists; `balanced-v2` and
`legacy-balanced-v1` remain available for compatibility.
`openai-priority-v1` orders OpenAI-production then Anthropic-production
portfolios; `claude-priority-v1` reverses them. `cursor-priority-v1` has both
orders with exact Cursor routes first and the older effort policy.
`balanced-v2` retains the high-effort native-first portfolio. Each portfolio has ordered per-role
candidates and distinct production/checking families; no partial plan is
valid. Temporary overrides accept only reason `credits_exhausted`, a TTL from
1 through 604800 seconds, and scope `account-route`, `provider-family`,
`model`, or `route`. Subscription quota telemetry is not complete enough to
drive automatic activation.

Contract 1.4 adds explicit v1-plan migration and operator-approved mid-ticket
fallback. The exact failed route is excluded, prior contributor-family history
is enforced, and a one-use Linear comment binds the validated partial-work
snapshot and append-only journal revision. See
[model-routing.md](model-routing.md) for the role priorities and complete flow.

Contracts `1.1.0` through `1.5.0` keep one-ticket behavior by default and accept
`MAX_CONCURRENT_TICKETS` only from `1` through `4`. Contract 1.6 defaults to
`4` and accepts `1` through `6`. Above one, the dispatcher uses
`claim`, `renew`, and `release`, and supplies the matching `--lease` to
preflight, next-stage, run, and ticket-attest. Capacity refusal is deterministic,
and duplicate ticket or lease identity fails closed.
Maintenance blocks claims and renewals but matching owners may still release
leases so the product can drain. Activation and rollback refuse until all
leases are gone. Leases expire after 15 minutes unless renewed, but expiration
never makes them available to another dispatcher and a stale record still
occupies capacity. This one product setting is the coupled ticket-worktree and
provider-call capacity; there is no separate provider-capacity setting. The
product-wide provider lock remains held for native subscription, Cursor CLI,
and every other legacy provider interval. An exact Contract 1.6 API route may
bypass it only when selected by the owner-only isolated-v1 activation file;
missing or malformed activation never enables parallel provider work.

The `factory-supervisor` skill is a one-shot adapter over `dispatch-plan`: one
wakeup claims at most one ticket and starts at most one ephemeral dispatcher
child. Autonomous claims require `MAX_CONCURRENT_TICKETS` above one so an opaque
lease can remain in memory and accompany the child. `WAIT` and `ESCALATE` never
prepare a worktree or start a child. When `next-stage` first authorizes Reviewer,
`ticket-pr` creates or reuses exactly one open PR at the clean pushed branch
head before review and reports `wait` without launching a role while required
checks are pending. Completed failures are Reviewer evidence, not approval.
Before Narrator, the dispatcher invokes it again and requires `ready`; the
helper binds successful required checks and latest Reviewer evidence to the
exact current head. It cannot approve or merge.

Contract 1.6 defines `scripts/provider-runtime.py` as the coupling boundary for
the owner-only SQLite coordinator and ephemeral container executor. Admission
uses a short `BEGIN IMMEDIATE` transaction and states
`prepared → reserved → GO → submitted → terminal`. The worker receives a
sanitized source snapshot and immutable input; its identity binds ticket, role,
attempt, base SHA, route, policy, image, input, source, and command. Unknown
post-GO outcomes retain the reservation and slot. The owner-only credential
broker substitutes provider credentials at an exact configured HTTPS endpoint
using a short-lived token bound to attempt, route, model, reservation, expiry,
and request count. The trusted runtime consumes the token and relays only the
bounded result into the networkless worker. Broker status never reports tokens
or raw credentials, and terminalization revokes the token. A release-owned lock
pins the worker image digest. Successful patch output is not trusted directly:
the host artifact controller verifies its executor hash, full identity,
telemetry, base, paths, protected-path policy, and temporary-index application,
then applies and commits it under a per-ticket lock. Each route remains
disabled until its activation evidence and owner configuration are installed;
legacy serialized runs remain available for non-activated routes. Under
maintenance, recover a stale record explicitly with:

```bash
bash scripts/factory-kit.sh recover-lease \
  --project "$PROJECT" --product "$PRODUCT_REPO" --ticket T-123
```

`contract` returns the manifest. `doctor` returns
`nysa.software-factory.hermes-doctor/v1`. `preflight` and `next-stage` preserve
the selected helper's exit code and wrap its redacted text in versioned JSON.
`next-stage` also returns `action` and `detail`.
Contract 1.2 requires the exact ticket worktree for preflight and sequencing,
adds `ticket-state` as the only path that materializes reconciled operator
fields or commits role-stage transitions, and adds `project-ledger` as the only
path that projects effective runtime accounting into the tracked durable
ledger. Trusted write helpers accept only the exact product origin from the
active certification receipt. Contract 1.2 stops in Review after bundle
creation. Both ticket-state transition and materialization refuse Awaiting
Approval, Approved, and Done until dedicated trusted bundle and merge/deploy
attestation paths are added; `next-stage` does not authorize `AWAIT-MERGE`
under 1.2.

Contract 1.3 adds `ticket-attest` with exact actions `bundle`, `approval`, and
`done`. Bundle/approval require the exact clean ticket branch and remote tip;
done requires clean `chore/tNNN-closeout` based on `origin/main`. The helper
parses `GH_REPO` and `DONE_REQUIRED_CHECKS` from `factory/PROJECT.env` as data,
requires the exact configured `AUTO_MERGE_METHOD`, uses only the receipt-bound
origin and profile-derived `GH_TOKEN`, and refuses
ambiguous PRs, changed evidence, stale approval, unconfirmed auto-merge, merge
commits absent from main, or unsuccessful post-merge contexts.
At concurrency two, all three actions require the matching lease. Done also
requires a pristine closeout branch exactly at `origin/main`, validates the
protected approval chain and merged PR head, and rejects ambiguous status/check
name collisions. It then creates or reuses one exact factory-owned closeout PR
and requests protected auto-merge with no bypass or second business approval.
Network retries reuse and revalidate the same closeout commit instead of
projecting or committing twice.

After that PR merges, `next-stage` returns `COMPLETE` only when the strengthened
effective-ticket reader validates attested Done on protected main. The
dispatcher then invokes the existing trusted lease `release`; PR creation or
an auto-merge request alone never releases it. Linear sync projects that same
protected-main Done state.

The release also contains one non-launcher migration utility,
`scripts/legacy-closeout.py`, for the bounded Contract 1.2 backlog present at
the Contract 1.3 cutover. It has no Hermes command or permanent compatibility
surface. From an exact protected-main basis it validates trusted Git/GitHub
history, app-bound successful checks, settled accounting, classification,
cutoff, and an exact request, then deterministically writes a distinct
`factory/migrations/contract-1.3/` authorization/receipt batch, terminal ticket
projections, and target pin. It never commits, pushes, merges, uses auto-merge,
or mutates Linear. The batch is authoritative only after the operator manually
merges the single protected product PR. The same internal validator is used by
effective-ticket reading, `next-stage`, and activation; plain Done and partial
or conflicting evidence are invalid.

`scripts/terminal-backfill.py` is a second non-launcher migration utility for
the exact authorized pre-contract terminal-Done batch. It writes the separate
`factory/migrations/contract-1.3-terminal-backfill/` evidence set, preserves
historically absent bundles and Kit-SHAs as null, and binds both implementation
and closeout PR ancestry plus each era's authentic app-owned checks. It shares
the same manual protected-merge authority and fail-closed terminal reader, but
does not alter or extend the first legacy-closeout batch.

T-013 through T-016 predate separate `policy` and `app-tests` check jobs. Their
only bounded migration class requires the authentic app-bound aggregate `ci`
and `test-immutability` checks plus independent criteria-audit and current
combined-test digests. No other ticket may use that historical check profile.

`project-ledger` refuses any active or ambiguous entry under
`factory/.active-runs/` and any `factory/runs/*.pid` record. Reconcile those
records under maintenance before close-out; projection never guesses that a
claim or PID is stale.

`run` and `reorder-test-fixes` are process boundaries rather than JSON
commands. Their arguments and behavior are still compatibility-sensitive and
defined in the manifest. Unknown schemas, categories, actions, or contract
versions are stop conditions.

The doctor is diagnostic and read-only. It uses temporary files for bounded
CLI version probes, reports credential presence only, redacts secret-bearing
keys and credential URLs, and never authenticates or repairs anything.
`warning` does not mean authentication succeeded. `error` blocks dispatch.

The route catalog keeps transport, gateway, inference provider, family,
account route, selectable model ID, and expected reported identity separate.
The selected ID is sent to the CLI; the independently reported identity must
match when one is expected. Cursor's exact OpenAI and Anthropic model IDs are
therefore separate routes, not a shared adapter default.

Kimi K2.6 is cataloged only as a disabled experimental route using Claude CLI
through OpenRouter to Moonshot. It is absent from all profiles and no live or
billed pilot has been performed. Rotate its credential before any pilot.
Direct same-UID token observation remains possible without a credential broker
or OS-level isolation.

## Install and certify an exact release

Set the candidate to a full commit already merged into `origin/main`.

```bash
KIT_REPO="$HOME/Projects/nysa-company/software-factory"
PRODUCT_REPO="$HOME/Projects/example-product"
PROJECT="example"
SHA="$(git -C "$KIT_REPO" rev-parse origin/main)"

bash "$KIT_REPO/scripts/factory-kit.sh" install \
  --repo "$KIT_REPO" \
  --sha "$SHA"
```

Installation verifies canonical origin identity, full-SHA ancestry on fetched
`origin/main`, required GitHub checks, and the candidate tree. It runs the kit
suite, repository check, and secret scan in a disposable writable checkout,
then archives the verified Git object into
`~/.factory/kits/releases/$SHA` and seals it read-only. Existing valid
installs are idempotent; partial or corrupt paths fail closed. A successful
fresh install also writes owner-only, expiring suite evidence beside the
trusted install manifest.

The product must be clean, pinned to the same SHA, and define one executable,
repository-contained path:

```text
CERTIFY_SCRIPT=factory/certify.sh
```

Then certify:

```bash
bash "$KIT_REPO/scripts/factory-kit.sh" certify \
  --project "$PROJECT" \
  --product "$PRODUCT_REPO" \
  --sha "$SHA"
```

Certification first verifies the manifest-backed sealed release. It reuses the
install suite result only when its release, current physical tree, host,
OS/architecture, suite-definition, tool-version, and configured lifetime
bindings all match and it remains unexpired. Otherwise it safely reruns the
suite against a disposable writable copy and atomically refreshes the evidence
after all suite and release checks pass. Missing or invalid reusable evidence is
a cache miss, not a certification failure; a failed fresh suite still fails.
The product's certification script always runs with a fixed working directory,
sanitized environment, and timeout. A passing receipt binds:

- project, kit SHA/tree/canonical origin, and contract version;
- product absolute path, origin, Git tree, `KIT_PIN` hash, and `PROJECT.env`
  hash;
- host, operating system, architecture, previous generation, and required
  check results;
- the exact kit-suite evidence ID, digest, definition, lifetime, and whether it
  was reused;
- creation and expiry timestamps.

Receipts are mode `0600`. Their default lifetime is 86,400 seconds and may be
changed with `FACTORY_KIT_RECEIPT_TTL_SECONDS`. Activation rechecks receipt
expiry and every bound value. Product, pin, config, host, release, or contract
drift requires recertification. Suite evidence also defaults to 86,400 seconds,
may be changed with `FACTORY_KIT_SUITE_EVIDENCE_TTL_SECONDS`, and caps receipt
expiry so product proof cannot outlive kit-suite proof. Receipt schema 2 and
certification tool version 2 intentionally reject older incompatible receipts.

The implemented receipt does not yet bind live Hermes profile files,
LaunchAgent hashes, or every CLI path/version. The real-Hermes canary and
cutover evidence cover those machine-specific surfaces.

## Plan and activate

Candidate installation and certification do not interrupt the live factory.
Before cutover:

1. Confirm the candidate passed required Linux and macOS CI.
2. Complete the real-Hermes canary below.
3. Confirm no role run is active and no nonterminal ticket has a conflicting
   `Kit-SHA`.
4. Commit the candidate `factory/KIT_PIN` through the product's protected PR
   flow and confirm the merged product tree is the certified tree.
5. Create `factory/MAINTENANCE`. This blocks preflight, sequencing, launches,
   and release-sensitive reordering.
6. Wait for existing role processes and dispatcher leases to drain. Do not
   delete PID or lease records to make the check pass.

Validate without mutation:

```bash
bash "$KIT_REPO/scripts/factory-kit.sh" plan \
  --project "$PROJECT" \
  --product "$PRODUCT_REPO" \
  --sha "$SHA"
```

Activation requires `MAINTENANCE` to exist before it takes the product launch
lock. Launches check maintenance before the lock, after taking the lock, and
again before the task GO signal. Therefore either a launch passes the barrier
first or activation drains it; no new task can cross after maintenance is
published.

```bash
bash "$KIT_REPO/scripts/factory-kit.sh" activate \
  --project "$PROJECT" \
  --product "$PRODUCT_REPO" \
  --sha "$SHA"
```

The journal phases are:

```text
prepared
maintenance_published
launch_drained
services_stopped
activation_record_switched
integration_bundle_switched
services_started
healthy
committed
```

`rolled_back` is terminal for an aborted or reversed generation. The release
manager atomically writes the journal and `active.json`. In the current
implementation, the service and integration phase names are transaction
checkpoints; `factory-kit.sh` does not itself call `launchctl`, copy profile
files, or perform the external health smoke. The operator must stop/start the
factory-only Hermes profile and reconciler, switch any compatibility bundle,
and collect health evidence around activation.

If activation is interrupted, leave maintenance in place and run:

```bash
bash "$KIT_REPO/scripts/factory-kit.sh" reconcile \
  --project "$PROJECT" \
  --product "$PRODUCT_REPO"
```

Before the pointer-switch phase, reconcile restores the previous state. At or
after the switch, it commits only when the active generation and receipt still
validate; otherwise it restores the previous generation.

## Real-Hermes canary

A real canary has not yet been run. Do not use contract-test success as a
substitute.

1. Create a dedicated sandbox product repository and separate profile, for
   example `~/.hermes/profiles/factory-canary`. Give it its own project slug,
   factory state, tickets, ledger, and registry.
2. Install `SOUL.md` and `skills/factory-dispatch/SKILL.md` from the candidate
   release into the canary profile. Adapt the factory gateway LaunchAgent with
   a distinct label and `--profile factory-canary`.
3. Do not copy the production profile's `.env`, secret files, board mapping,
   ledger, registry, or LaunchAgent. Use no credentials when possible; if a
   probe requires one, create a separate least-privilege sandbox credential.
4. Point the canary registry's `PRODUCT_ROOT` only at the sandbox product.
   Registry files are data and must contain no secrets.
5. Start the canary with the real installed Hermes binary and its normal
   profile-loading/LaunchAgent mechanism. Mock only task adapters and external
   actions.
6. Through Hermes, verify `contract --json`, `doctor --json`, preflight,
   `next-stage`, and one mock role launch. Confirm the run manifest names the
   candidate SHA/tree and physical release.
7. Stop and remove the canary LaunchAgent after capturing redacted evidence.

Run the full canary for the first cutover and any compatibility-sensitive
change. Compatible releases may use contract tests plus doctor probes only
after the first real canary and cutover have been accepted.

## Bounded outage and acceptance evidence

Preparation, install, certification, and canary work happen beside the active
release and have no planned control-plane outage. The measured outage starts
when `MAINTENANCE` is published and ends only after the factory profile and
reconciler are healthy, doctor has no error, a sandbox smoke passes, and the
operator removes `MAINTENANCE`. Existing task processes must drain before
activation; the dashboard and primary Hermes profile remain untouched.

Operational targets:

- planned control-plane outage: at most 5 minutes;
- restore previous known bits after a failed health decision: at most
  5 minutes, with maintenance still present;
- full rollback RTO: at most 30 minutes, measured until the protected
  `KIT_PIN` revert is merged, the previous tuple is revalidated, health and
  sandbox smoke pass, and maintenance is cleared.

Acceptance requires a redacted, timestamped record of:

- candidate and previous full SHAs/trees, CI URLs or check IDs, receipt ID and
  expiry, and certified product tree;
- separate canary profile/LaunchAgent identity, real Hermes version, launcher
  contract result, doctor JSON, sequencer result, and mock-run provenance;
- maintenance publication, drain confirmation, journal phase history,
  `active.json` generation, service PIDs, Linear sync freshness, and repeated
  health probes;
- outage duration and, for the drill, known-bits restore time plus full
  rollback RTO;
- protected pin PR/revert PR and proof that the previous release reads any
  state written by the candidate.

The first real-Hermes canary and Relay cutover were accepted on 2026-07-15:

- Hermes Agent `0.18.2` loaded the isolated `factory-canary` profile and
  LaunchAgent without production credentials. Contract, doctor, preflight,
  sequencing, and a mock planner run passed against release `45008d5`; its
  completed manifest bound contract `1.0.0`, kit tree `eff78c6`, product tree
  `272b741`, ticket lease, and the physical sealed path.
- Relay generation 1 activated release `3b63cc7` (kit tree `dbc9aff`) after
  protected follow-up fixes for certification and migrated ticket evidence.
  The production profile, registry, gateway, and reconciler then passed
  contract and doctor checks with a fresh Linear pull.
- The five-minute planned-outage target was not met. Activation at
  `05:38:18Z` reached post-clear doctor at `05:44:08Z` (5m50s), and the full
  maintenance/drain interval was longer because several fail-closed defects
  required new protected releases. Treat this as measured rollout evidence,
  not an accepted outage SLO.
- The first-generation legacy restore proof completed at `05:45:22Z` and
  candidate recutover health at `05:45:58Z`. This accepts the documented
  first-migration exception only; formal release-manager rollback RTO remains
  unaccepted until generation 2 has a previous `active.json`.
- Relay generation 2 recertified the T-107-ready product against the same
  release and retained generation 1 as its formal previous activation. T-107
  then completed planner, spec-linter, test-author, builder, reviewer,
  Narrator, operator approval, protected PR #13, production `result=SAFE`,
  and Done closeout PR #14 with every run manifest on `3b63cc7`. The temporary
  setup PR #12 check override was removed immediately and is not protected-CI
  evidence; implementation and closeout checks passed under the restored
  no-bypass Ruleset.
- Relay generation 3 established current product tree `395918c` on retained
  release `3b63cc7` as the tested rollback baseline. Generation 4 then
  activated documentation-only successor `35c2e10` against protected Relay
  tree `b2f868f`; contract, doctor, readiness, product tests, services, Linear,
  and repeated probes passed. Green draft revert PR #16 reproduces `395918c`
  before rollback. The maintenance interval was 21m59s, so the five-minute
  target remains unaccepted. See
  `docs/operations/sofia-relay-upgrade-2026-07-15.md`.

## Rollback and retention

On failed activation health, keep `factory/MAINTENANCE` published and:

1. stop only the product's factory services;
2. run `reconcile` first if the activation transaction is interrupted and
   follow its terminal result;
3. merge the protected product revert that restores the previous full
   `KIT_PIN` and product tree, then update and verify the clean checkout; and
4. run rollback only if the candidate generation is committed and still
   active. If reconcile restored the previous generation or the candidate
   never committed, do not call rollback.

For a committed candidate after the protected pin/tree revert:

```bash
bash "$KIT_REPO/scripts/factory-kit.sh" rollback \
  --project "$PROJECT" \
  --product "$PRODUCT_REPO"
```

Rollback requires the previous pin and product tree to be restored already. It
atomically restores the previous activation record, verifies that the previous
sealed tree is present, and deliberately keeps `factory/MAINTENANCE`. It does
not restart services or prove old-code compatibility with candidate-written
state.

Relay generation 1 is a one-time exception because the legacy runtime had no
previous activation record. Its drill restores the hashed legacy
profile/registry/LaunchAgent bundle while maintenance remains published,
proves the preserved legacy product state is readable, and then reapplies the
candidate integration bundle. Do not invoke `factory-kit rollback` for this
case. Generation 2 and later must use the formal rollback command and protected
pin-revert flow.

After the previous activation record is restored, prove the previous release
can read candidate-written state, restart the factory-only services, run doctor
and sandbox smoke, and only then remove maintenance.

Automatic pruning is intentionally unavailable. Retain every release that is:

- active or the previous generation;
- named by a nonterminal ticket's `Kit-SHA`;
- referenced by a receipt or any activation journal;
- needed to read candidate-written persistent state;
- inside the rollback evidence window.

Manual removal is eligible only after a defined minimum age, multiple
successful real tickets on newer releases, a successful rollback drill,
verified backward readability or snapshot/restore evidence, and an audit that
no active record, ticket, receipt, or journal references the release.
