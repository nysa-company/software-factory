# Factory development lane

Use this macOS-only lane to exercise a committed Software Factory branch against a disposable synthetic product before the normal release process. It does not install, register, certify, or activate a kit and never uses Nysa product state.

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
