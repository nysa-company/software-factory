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

Review the returned exact plan and apply only its approval hash:

```bash
bash scripts/factory-kit.sh release resume \
  --project <project> \
  --sha <full-factory-sha> \
  --approve-hash <approval-sha256> \
  --approved-by <operator-id>
```

The plan binds Factory and product commits and trees, product origin and path,
the previous active generation, certification receipt, model policy,
qualification result, runtime pins, provider CLI pins, concurrency state, and
any approved ticket migrations. The signed release journal makes retries
idempotent and refuses changed inputs.

If a host cutover plan was never approved, abort that exact hash to restore its
captured maintenance state and release the machine-wide reservation:

```bash
bash scripts/factory-kit.sh release abort \
  --project <project> --sha <full-factory-sha> \
  --approve-hash <approval-sha256> --approved-by <operator-id>
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

Doctor reports only bounded readiness states. It never returns account data,
credential values, command output, or credential-bearing URLs.

## Qualification

`scripts/qualification-environment.py` prepares an owner-only, sealed
non-production environment for the exact candidate release. It uses its own
active record, controller state, provider state, operator map, runtime ledger,
ports, temporary home, and product checkout. It does not modify the installed
launcher or production active record.

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
