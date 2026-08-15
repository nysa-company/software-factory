# Factory runtime and safe releases

The Software Factory runs through one stable, Factory-owned command:

```text
~/.factory/bin/factory-launch <project> <command> [arguments]
```

Contract 2.0 has no external supervisor, profile, gateway, dashboard, or
secondary project registry. Native `launchd` scheduling calls the installed
launcher, which authenticates one active sealed release and delegates to the
deterministic controller and release-owned helpers.

## Runtime flow

```text
com.factory.controller.<project>
  -> ~/.factory/bin/factory-launch <project> reconcile --json
     -> ~/.factory/kits/projects/<project>/active.json
        -> ~/.factory/kits/releases/<full-sha>/scripts/factory-launch
           -> scripts/factory-controller.py
              -> deterministic helpers and pinned provider CLIs
```

The controller LaunchAgent wakes every 15 seconds and when the product run
directory changes. It does not interpret tickets, choose policy, approve work,
or call providers directly. All authority remains in authenticated product,
controller, receipt, lease, budget, and provider state.

## Contract

The public manifest is [`factory-contract.json`](../factory-contract.json).
Contract 2.0 identifies itself as `nysa.software-factory`; Doctor emits
`nysa.software-factory.doctor/v2`. The launcher source is
`scripts/factory-launch`, while `~/.factory/bin/factory-launch` remains the
stable installed entry point.

Inspect the active contract and runtime without bypassing the launcher:

```bash
~/.factory/bin/factory-launch <project> contract --json
~/.factory/bin/factory-launch <project> doctor --json
~/.factory/bin/factory-launch <project> reconcile --json
```

The manifest is authoritative for the complete command grammar. Common
operator commands include:

```bash
~/.factory/bin/factory-launch <project> watch --json
~/.factory/bin/factory-launch <project> operator-snapshot workflow --json
~/.factory/bin/factory-launch <project> models profiles --json
~/.factory/bin/factory-launch <project> models plan --json
~/.factory/bin/factory-launch <project> preflight \
  --ticket T-123 --role planner --workdir /absolute/ticket-worktree --json
~/.factory/bin/factory-launch <project> next-stage \
  --ticket T-123 --workdir /absolute/ticket-worktree --json
```

Commands reject unknown options and unsafe paths before invoking a helper.
Structured commands return JSON; long-running role commands preserve their
native process output and exit status.

## Machine-local authority

All active machine state lives under `~/.factory`:

- `bin/factory-launch` is the stable installed trust root.
- `bin/{claude,codex,agent}` are exact owner-managed provider CLI pins.
- `kits/releases/<full-sha>/` holds sealed, read-only release trees.
- `kits/manifests/<full-sha>.json` binds each installed release to its Git
  identity and canonical origin.
- `kits/projects/<project>/active.json` is the sole product-to-release binding.
- `kits/projects/<project>/activation-journal/` stores recoverable activation
  transactions.
- `kits/projects/<project>/controller/` stores controller claims and events.
- `kits/receipts/` stores expiring certification receipts.

`active.json.product_path` is the only product registry. The launcher validates
the project slug, file ownership and mode, physical containment, full release
SHA and tree, contract version, product Git identity, and sealed release path
before dispatch. Caller overrides cannot redirect an installed production
launcher.

## Install and release transaction

A release is always an exact protected-main commit. Installation verifies its
canonical origin, required GitHub checks, Git tree, isolated local smoke, and
release manifest before sealing it read-only.

Use the composite release transaction for production setup and upgrades:

```bash
bash scripts/factory-kit.sh release setup \
  --project <project> \
  --product /absolute/product-repo \
  --sha <full-factory-sha> \
  --profile <model-profile> \
  --operator-id <operator-id>
```

`setup` authorizes the returned exact sealed plan. Resume the current plan:

```bash
bash scripts/factory-kit.sh release resume \
  --project <project> \
  --sha <full-factory-sha> \
  --approved-by <operator-id>
```

The plan binds Factory and product commits and trees, product origin and path,
the previous active generation, certification receipt, model policy,
qualification result, runtime pins, provider CLI pins, concurrency state, and
any approved ticket migrations. The signed release journal makes retries
idempotent and refuses changed inputs.

Before a host cutover mutates a project, abort the current sealed plan to
restore its captured maintenance state and release the machine-wide reservation:

```bash
bash scripts/factory-kit.sh release abort \
  --project <project> --sha <full-factory-sha> \
  --approved-by <operator-id>
```

Abort is refused after the first project mutation; recovery is fix-forward from
that point.

Production upgrades require maintenance and a fully drained controller,
dispatcher leases, provider attempts, broker tokens, and qualification
consumer set. A machine-wide reservation prevents another setup or apply from
racing the reviewed cutover. The transaction switches every exact active
record, commits the Contract 2 floor, installs the sealed launcher, validates
all native controllers with Doctor, retires the reviewed old Factory jobs and
profile, then restores or clears only its own maintenance markers. A failed
first Contract 2.0 cutover stays in maintenance for fix-forward; it never
restores an unsupported release implicitly. Later Contract 2.x generations may
use the ordinary authenticated rollback flow.

## Native scheduling

Instantiate `scripts/launchd/com.factory.controller.plist.template` once per
product with its exact project, account home, and product path. The installed
job must have:

- label `com.factory.controller.<project>`;
- program `~/.factory/bin/factory-launch`;
- arguments `<project> reconcile --json`;
- `StartInterval` of 15 seconds;
- a `WatchPaths` entry for `<product>/factory/runs`;
- no credential-bearing environment entries.

Doctor validates the persisted plist, loaded job identity, disabled state,
program and arguments, interval, watch path, and log paths. The optional
incident reporter remains a separate narrow LaunchAgent and never participates
in reconciliation.

## Credentials

The launcher starts helpers under a fixed clean environment. Provider
credentials remain in their existing owner-local stores and are never copied
into product worktrees or durable evidence.

For GitHub HTTPS mutations, the launcher uses one ownership- and mode-validated
fixed `gh` executable only for the migration or fallback operation that needs
it. The clean child environment uses the account's fixed `$HOME/.config/gh`
credential store; it never extracts or forwards a token. Read-only and provider
execution paths remain credential-free.

Doctor reports only bounded readiness states. Cheap CLI probes and model/provider
readiness have separate bounded windows so a healthy multi-route readiness scan
is not held to a version probe's deadline. A timeout or malformed readiness
response remains a typed error report rather than breaking Doctor's JSON
contract. Doctor never returns account data, credential values, command output,
or credential-bearing URLs.

## Qualification

`scripts/qualification-environment.py` prepares an owner-only, sealed
non-production environment for the exact candidate release. It uses its own
active record, controller state, provider state, operator map, runtime ledger,
ports, temporary home, and product checkout. It does not modify the installed
launcher or production active record.
Before publishing that state, it proves every selected remote ticket branch is
absent, descends from protected main, or has an exact protected authorization
for canonical recovery. The v1 authorization remains limited to pre-provider
Ready/pin history. A v2 authorization additionally binds the exact earlier
qualification generation, Factory SHA, product SHA, and branch head, and admits
only a durable Ready base or its Ready receipt, one route pin, and ticket-only
qualification work.
Dispatch repeats the observation before the exact-head CAS reset; application
changes are never reset through this path.
Preparation and dispatch both accept an external dependency only through the
shared protected dependency predicate; dependency-only fulfillment never
counts a selected qualification target as Done.
If dispatch loses the response around that reset, replay recognizes only the
canonical reset commits and exact in-progress cleanup. A Backlog reset is made
Ready again through the normal operator receipt, starting from the exact reset
SHA; unrelated local commits, dirty paths, or a changed remote remain blocked.
When a later generation reauthorizes that Ready head, preparation and dispatch
recursively validate every prior Ready epoch and canonical reset pair before
repeating the same exact-head recovery.
Role-control evidence that predates the sealed qualification product SHA is
history, not evidence for the candidate run. The launcher binds that SHA and
the shared sequencer, controller, state machine, Reviewer reconciliation,
attestation, and qualification rollback paths all use the same read-only epoch
projection. Protected controls must remain an exact ordered prefix; missing,
rewritten, reordered, or interposed history blocks without editing the ticket.

After preparation, one sealed command owns deterministic progress to the next
authenticated boundary:

```bash
/private/tmp/nysa-sf-qualification.<lane>/releases/<factory-sha>/scripts/factory-launch \
  <project> qualification-run --json
```

It requires Doctor `ok`, the exact bounded runtime-only warning produced by
active qualification leases, or—only for a successor—the exact selected-ticket
`prior_kit_receipt` warning that the controller itself must migrate. A warning
from one prior candidate is accepted only when it covers the full selected
cohort; the controller still authenticates and migrates every receipt. It
performs the one mandatory controller restart in a new process, runs the ordinary
controller/state machine, applies the existing sealed batch route migration
when every selected successor claim is at that exact boundary, and invokes the
existing reducer only after a nonempty set of terminal completion results. The
batch reads its in-flight authorization from the sealed qualification product
SHA; production continues to require protected-main authority. The preview
digest stays inside the command; no human hash handoff is needed.
Every other warning remains a typed block. Rerun after a named external or
operator input changes; the driver never hand-edits tickets, claims, leases,
receipts, passports, journals, or provider state.

Qualification must prove:

1. the release installs and certifies in an isolated home;
2. the Factory launcher resolves only the sealed candidate;
3. the native controller reconciles selected tickets through terminal
   boundaries;
4. provider admission, accounting, evidence, and approval boundaries remain
   intact;
5. interruption and replay return the same authenticated result;
6. no deprecated profile or secondary registry is created.

On macOS, also load the native controller plist in the isolated home, observe
one reconciliation, and unload it. This is the surviving scheduler smoke; no
second canary framework is required.

## Failure and recovery

- `factory-kit pause` publishes maintenance and drains new admission.
- `factory-kit reconcile` resumes or safely resolves an interrupted activation
  journal.
- `factory-launch <project> reconcile --json` resumes controller claims from
  durable evidence without replaying completed provider work.
- `factory-kit rollback` accepts only a retained, sealed Contract 2.x release
  whose product pin and tree have already been restored and revalidated.
- `scripts/kill-switch.sh` stops recorded runs and Factory schedules while
  preserving evidence needed for diagnosis.

Never hand-edit `active.json`, receipts, journals, leases, provider state, or
sealed releases. Keep maintenance published when any identity, health, or
credential check fails.

## Verification and retention

Before installation, the exact protected-main SHA must pass full required Linux
and macOS CI. Locally, run:

```bash
bash ci/factory-contract-test.sh
bash ci/test-all.sh --changed-or-defer origin/main HEAD
scripts/repo-check
scripts/secret-scan
```

Retain the active release, at least one proven Contract 2.x rollback release,
their install manifests, consumed receipts, activation journals, runtime pin
evidence, and qualification evidence. Older sealed releases remain immutable
audit records but are not selectable after the Contract 2.0 cutover.

The planned control-plane outage begins when maintenance is published and ends
when maintenance is cleared after Doctor passes. Target five minutes or less;
otherwise keep the Factory in maintenance and record the miss honestly.
