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

This runs four identity-bound mock subscription commands through one mock account in the Contract 1.7 CLI runtime and existing transactional coordinator at once, then completes four disposable Planner-through-Narrator lifecycles. The policy, database, worktrees, project identity, ports, timeline, home, and temporary files are unique to the owner-only lane root. The command proves temporal overlap, retains reservations until trusted-host terminalization, drains them, leaves each ticket clean and locally pushed in `Review`, and must finish in less than 15 minutes. It does not use credentials or permit a real provider call.

Focused provider tests separately hold four loopback broker requests open, cancel one by controller signal, admit a replacement only into that released slot, and verify timeout cleanup. Capacity is released only after token revocation and request drain are both proven.

## Four-call subscription canary

After the mock proof passes and no provider call is active, plan and run the one-use canary:

```bash
bash scripts/factory-dev-lane.sh subscription-plan
bash scripts/factory-dev-lane.sh subscription-run \
  --root <root-from-plan> --approve-hash <hash-from-plan>
```

The canary uses one existing authenticated Codex subscription CLI session—never an API key—for four same-account Codex calls. It copies only the Codex session into the lane-local home, reserves $0.25 per synthetic ticket ($1 total), requires all four calls to overlap, validates exact output, charges the full conservative reservation, and rejects any worktree mutation. Planning binds the Codex executable path, bytes, version, provider identity status, policy, activation, lane nonce, and product tree; execution consumes that approval before admission and stops if another task-bearing subscription CLI process is active.

The subscription canary copies only its Codex CLI session; product lanes copy
their three configured CLI sessions into the owner-only lane root once.
Readiness, version evidence, approval hashing, and role execution then use the
same clean environment and only those copied files from the lane root as their
working directory; ambient authentication variables, the caller's working
directory, and the external Cursor session home are not consulted. If a
required copied session is unavailable, the lane stops before consuming
approval, claiming a lease, reserving budget, or submitting a task.
Each readiness probe gets three attempts with a one-second delay between
misses so a short CLI session-state transition cannot exhaust every retry.
Retained-product resumes run that probe before hashing the plan and again
before validating it for execution; the internal run reuses the second result
instead of immediately probing the same session a third time.
Every failed role attempt is terminal and charged conservatively. The scheduler
does not retry automatically, including for a pre-submission authentication or
model-availability miss. After the lane drains it reports the failed ticket set
and an exact `product-resume-plan` command as the next control step, together
with the failed stage, completed roles, remaining ticket budget, and retained
root. That explicit same-lane resume revalidates readiness, evidence, clean
heads, routes, envelopes, budget day, and the mechanical next stage before
issuing a new one-use approval. Every batch also writes an owner-only timing
report with coordinator admission/GO/submission/terminal timestamps, elapsed
time, successful-role replay count, and maximum provider overlap.

The product scheduler launches every eligible ticket without interpreting
provider capacity. The coordinator owns atomic admission and may wait up to
five minutes only for transient concurrency capacity in this marker-bound
development lane. The runner keeps the ticket lease heartbeat active and
releases the product launch lock while waiting, then reacquires it and
revalidates controls before proceeding. Budget, policy, identity, and rate
denials remain immediate. Development activation permits four calls for one
Codex account; Cursor and native Claude remain capped at two, and the native
Claude circuit breaker remains in force.

## Isolated product proof and resume

`product-plan` accepts one to four tickets after the dedicated four-call mock and subscription proofs establish the capacity ceiling. A seeded retry may select one to four unfinished tickets, so completed siblings are not rerun. The source must be a clean isolated worktree; the canonical Nysa checkout is refused. The lane clones it into a private product, replaces its remote with a local bare origin, and has no GitHub or Linear route.

Seeded retries require an owner-only, single-link accounting manifest bound to the exact seed-bundle digest and approved base SHA. They also require an owner-only lineage record in one shared artifact directory; it binds the manifest digest, a lineage ID derived from the base and complete historical ticket set, and the previous manifest digest. Create it only with `product-seed-lineage --accounting <manifest> --output <new-record>`; add `--parent-accounting <previous-manifest>` for a successor. The helper requires all artifacts to share one owner-only directory and will not overwrite a record. Caller-selected IDs are rejected. An atomic lineage-head advance permits exactly one child of a cumulative accounting snapshot, so separately authorized sibling lanes cannot both spend from stale totals. A busy, reused, detached, or stale lineage stops before lane creation. Its full historical ticket map is retained even when only a subset resumes. A seed resets role sequencing to `Ready` and discards prior role verdict lines because runtime role evidence is lane-bound and cannot be inferred safely from Git history alone. V2 keeps the default $100 ticket and $500 aggregate ceilings; an explicit operator-authored v3 record permits only the bounded $200 ticket and $700 aggregate development-proof ceilings. V4 permits an operator-authored per-ticket cap map up to $350 per ticket with an exact $1,000 or $1,500 aggregate ceiling. V3 and V4 also bind one authorization nonce and UTC budget day; planning consumes that nonce through the same lineage transaction, and day drift stops before reservation. Prior reservations reduce both limits, and the resulting per-ticket envelope remains the coordinator's atomic admission cap. Missing history, reuse, an exhausted selected ticket, aggregate exhaustion, unauthorized limits, bundle/base/lineage drift, duplicate tickets, seeded symlinks or submodules, and unsafe ticket files fail before provider execution.

For sequential protected-base refreshes, keep using the existing committed
`ticket-refresh/v1` attestation path. It already invalidates the old approval
and requires a fresh Reviewer and Narrator bound after the exact merge. This
lane does not add a second refresh or checkpoint format.

`product-export` binds each application patch to the latest successful
Reviewer head, rejects later non-Factory changes, and excludes the complete
reserved `factory/` namespace. The exact Git bundle retains retry and audit
history only; it is never an application artifact. Apply only the projected
patch to a fresh isolated product branch, then refresh and reverify it against
current protected main. When successful siblings finish in different retained
lanes, `product-export --tickets T-NNN,...` exports only the named completed
subset while applying the same role-evidence and Reviewer checks.

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
