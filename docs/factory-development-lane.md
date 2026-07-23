# Factory development lane

Use this macOS-only lane to exercise a committed Software Factory branch against a disposable synthetic product or an explicitly isolated product-source worktree before the normal release process. It does not install, register, certify, or activate a kit.

## Mock lifecycle

From a clean, committed Software Factory checkout:

```bash
bash scripts/factory-dev-lane.sh mock
```

The command creates a private `nysa-sf-dev.*` directory directly under `TMPDIR`, clones the exact factory commit into it, creates a separate product and local bare Git remote, and runs Planner through Narrator with mock adapters. Seatbelt denies network access. The ticket must finish clean and pushed locally in `Review`, with the sequencer returning `AWAIT-OPERATOR`, in less than 15 minutes. A successful run cleans itself; add `--keep` to retain it for inspection.

## Four-ticket isolated mock

```bash
bash scripts/factory-dev-lane.sh mock-concurrency
```

This runs four identity-bound mock subscription commands through the Contract 1.7 CLI runtime and existing transactional coordinator at once, then completes four disposable Planner-through-Narrator lifecycles. The policy, database, worktrees, project identity, ports, timeline, home, and temporary files are unique to the owner-only lane root. The command proves temporal overlap, retains reservations until trusted-host terminalization, drains them, leaves each ticket clean and locally pushed in `Review`, and must finish in less than 15 minutes. It does not use credentials or permit a real provider call.

Focused provider tests separately hold four loopback broker requests open, cancel one by controller signal, admit a replacement only into that released slot, and verify timeout cleanup. Capacity is released only after token revocation and request drain are both proven.

## Four-call subscription canary

After the mock proof passes and no provider call is active, plan and run the one-use canary:

```bash
bash scripts/factory-dev-lane.sh subscription-plan
bash scripts/factory-dev-lane.sh subscription-run \
  --root <root-from-plan> --approve-hash <hash-from-plan>
```

The canary uses existing authenticated Cursor, Codex, and Claude subscription CLIs—never API keys—for one Cursor, two Codex, and one Claude call. It reserves $0.25 per synthetic ticket ($1 total), requires all four calls to overlap, validates exact output, charges the full conservative reservation, and rejects any worktree mutation. Cursor remains capped at one process because its temporary bridge is shared. Planning binds executable paths, bytes, versions, provider identity status, policy, activation, lane nonce, and product tree; execution consumes that approval before admission and stops if another task-bearing subscription CLI process is active.

Subscription and product lanes copy the three CLI session files into their
owner-only lane root once. Readiness, version evidence, approval hashing, and
role execution then use the same clean environment and only those copied
files; ambient authentication variables and the external Cursor session home
are not consulted. If a copied session is unavailable, the lane stops before
consuming approval, claiming a lease, reserving budget, or submitting a task.

## Isolated product proof and resume

`product-plan` requires four tickets for a fresh proof. A seeded retry may select one to four unfinished tickets, so completed siblings are not rerun. The source must be a clean isolated worktree; the canonical Nysa checkout is refused. The lane clones it into a private product, replaces its remote with a local bare origin, and has no GitHub or Linear route.

Seeded retries require an owner-only, single-link accounting manifest bound to the exact seed-bundle digest and approved base SHA. Its full historical ticket map is retained even when only a subset resumes. V2 keeps the default $100 ticket and $500 aggregate ceilings; an explicit operator-authored v3 record permits only the bounded $200 ticket and $700 aggregate development-proof ceilings. V3 also binds one authorization nonce and UTC budget day; planning consumes that nonce in the owner-only artifact directory before creating a runnable lane, and day drift stops before reservation. Prior reservations reduce both limits. Missing history, reuse, an exhausted selected ticket, aggregate exhaustion, unauthorized limits, bundle/base drift, duplicate tickets, seeded symlinks or submodules, and unsafe ticket files fail before provider execution.

`product-export` binds each application patch to the latest successful
Reviewer head, rejects later non-Factory changes, and excludes the complete
reserved `factory/` namespace. The exact Git bundle retains retry and audit
history only; it is never an application artifact. Apply only the projected
patch to a fresh isolated product branch, then refresh and reverify it against
current protected main.

## Real Cursor lifecycle

The real probe is an explicit release gate. Put an authenticated Cursor `agent` binary on `PATH`:

```bash
bash scripts/factory-dev-lane.sh cursor-plan

bash scripts/factory-dev-lane.sh cursor-run \
  --root <root-from-plan> --approve-hash <hash-from-plan>
```

`cursor-plan` gives only Cursor subprocesses the normal session home. Current Cursor builds cannot read their authenticated Keychain session when nested under macOS Seatbelt, so the real lane enables Cursor's own `--sandbox enabled` boundary instead; mock mode retains the stricter outer Seatbelt boundary. The synthetic product also denies agent-initiated `Shell(security)`. The one-use approval binds both session files, the lane nonce, factory and product trees, route plan, Cursor version, resolved executable path, and executable bytes. Cursor's hardcoded `/tmp/.cursor` scratch path is redirected through an ephemeral symlink into the lane and removed on every invocation exit. `cursor-run` consumes the approval before provider execution and stops on drift. The reviewer must stay read-only and report `APPROVE`; the final state remains `AWAIT-OPERATOR`.

## Cleanup and boundaries

```bash
bash scripts/factory-dev-lane.sh clean --root <root>
```

Cleanup accepts only the original owner-only lane directory beneath its creation `TMPDIR`, with an unchanged owner, inode, device, marker, and permissions. Failed runs are retained for diagnosis.

Before any isolated attempt, every runtime input is checked lexically and physically beneath the validated lane root and against the production denylist. The lane refuses canonical Nysa, production factory, production Hermes, and LaunchAgent paths. It creates no production receipt or activation record, has no Linear or GitHub integration, and cannot become a production release. The normal protected-main CI, sealed installation, live Cursor canary, product certification, registration, activation, and legacy serialized-provider path remain unchanged.
