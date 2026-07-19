# Architecture

## Engine model

The kit is installed as immutable exact-SHA releases and shared by every product. Product repositories contain only their own state. Every live run separates the selected physical release (`KIT_DIR`) from `FACTORY_ROOT` (the product repository's tickets, ledger, envelope, and configuration):

```bash
~/.factory/bin/factory-launch <project> run \
  --role <role> --ticket <T-NNN> \
  --prompt-file <release-role-path> --workdir <ticket-worktree> -- "task text"
```

- **Kit:** scripts, adapters and version pins, role contracts, workflows, runbooks, and CI templates. Fixes land through reviewed PRs, but a merge does not activate them.
- **Product repository:** `factory/` state (including initiatives and tickets), product documentation, instantiated CI, GitHub rules, and deploy credentials. All products share the Software Factory Linear team; each initiative gets a Linear Project.
- **`factory/KIT_PIN`:** exactly one lowercase, full 40-character certified kit SHA. External products fail closed when it is missing, malformed, or different from the physical release.
- **`factory/PROJECT.env`:** product name, exact `GH_REPO`, protected test paths, worktree location, ticket branch prefix, contract-1.3 `DONE_REQUIRED_CHECKS` (a unique comma-separated list of exact post-merge status/check names), and required `AUTO_MERGE_METHOD` (`squash`, `merge`, or `rebase`).

Per-product limits live in each product's `ENVELOPE.env`; the machine limit in `~/.factory/global.env` caps aggregate spend.

Runtime accounting is immutable per run: `factory/runs/<run_id>.meta` records the reservation, durable pre-GO marker, terminal state, cost, and basis. Preflight and the first normal Linear reconciliation durably initialize the ignored `factory/runs/` root before reducing accounting. The reducer opens that root without following symlinks and accepts only regular, single-link manifests. For backward readability, a legacy durable reservation immediately followed by its terminal row reduces to the terminal row; other duplicate run IDs still fail closed. The ignored `factory/runtime-ledger.csv` is a deterministic effective view over those manifests and tracked `factory/ledger.csv`; only launcher command `project-ledger` writes the tracked ledger from a clean `chore/tNNN-closeout` worktree after every product run is terminal and accounted. Projection refuses any active or ambiguous claim under `factory/.active-runs/` and any `factory/runs/*.pid` record; operators must reconcile those records before close-out rather than guess whether a process is stale.

Each ticket-and-role run takes an atomic `mkdir` claim under
`factory/.active-runs/` before it creates a manifest. A conflicting or
abandoned claim always refuses the launch; ordinary launch never guesses that
a PID is stale or reclaims the directory. Cleanup removes only the exact owner
record it created. The wrapper also keeps provider output on an unlinked open
descriptor until the provider exits, then publishes the ignored `.out`
artifact. Missing, malformed, or oversized telemetry cannot reduce spend: a
post-GO run keeps the full reservation and zero turns when its cost data is not
usable.

Before creating a manifest, every run acquires a product-level control lock and
holds it through provider exit and integrity verification. This temporarily
serializes all provider intervals even when two dispatcher ticket leases are
active. Any new or changed sibling manifest during that interval fails the role
and prevents sequencer advancement; only after verification may the wrapper
terminalize its own manifest. The lock owner records the wrapper PID, process
start identity, and a private ownership token. Ordinary launch waits only for a
validated live owner and never reclaims stale or malformed state; the kill
switch is the sole automated recovery path and quarantines only a provably
stale, unchanged lock after recorded processes drain.

When a machine-wide cap is configured, its global ledger lock also covers the
full provider interval, not just reservation. The wrapper validates the ledger,
persists a reservation, snapshots it, and verifies it before terminalization.
If the ledger changes while the wrapper still owns the exact lock, the wrapper
restores the snapshot; any persistent ledger, lock, claim, owned or sibling
manifest, or registered-checkout mutation fails the role.

These controls provide portable crash/race handling and detect mutations that
remain at the post-run check. They are not hostile-process isolation: the
provider CLIs run unsandboxed as the same OS user, which can alter user-owned
paths, signal the wrapper, or restore bytes before inspection. Preventing a
malicious same-UID process from authoring control state requires an OS boundary
such as a separate UID or enforced sandbox. The current wrapper therefore
claims fail-closed detection and conservative accounting, not literal
prevention against that actor.

The in-repository `conformance/` product is the only implicit-pin exception. It
must share the kit repository, Git common directory, and HEAD. This exception
exists for CI only. Live/external products and deployment certification require
an explicit `KIT_PIN`.

Model policy is kit-owned and certified by the same `KIT_PIN`. The route catalog
separates adapter, transport, gateway, inference provider, provider family,
account route, selection ID, and expected reported identity. Selection ID is
what the CLI is asked to run; reported identity is independently probed and a
mismatch is invalid. Cursor's exact OpenAI and Anthropic models are individual
routes, not one model inherited from a shared adapter.

Profiles contain ordered portfolios and ordered candidates per role. At the
ticket boundary, the first portfolio that resolves all six roles is selected.
`INVALID` or `UNKNOWN` hard-stops; only `UNAVAILABLE` advances to another
candidate or portfolio. Every portfolio declares distinct production and
checking families. The operator activates a profile by approving its exact
preview hash. Without an activation record, `legacy-balanced-v1` is the
default; its OpenAI-production/Anthropic-checking split is profile policy, not
a fixed architectural requirement.

```bash
# ~/.factory/global.env — no credentials in this file
export FACTORY_CURSOR_FALLBACK_ENABLED=0
export AGENT_CLI_CREDENTIAL_STORE="EXACT_STORE_TOKEN"
export CURSOR_AGENT_VERSION="EXACT_VERSION_TOKEN"
export CURSOR_OPENAI_MODEL=gpt-5.6-sol-high
export CURSOR_ANTHROPIC_MODEL=claude-sonnet-5-thinking-high
# Optional observational estimate only; ledger still keeps the full reservation.
export CURSOR_PRICING_SNAPSHOT_DATE=YYYY-MM-DD
export CURSOR_OPENAI_USD_PER_MTOK_IN="RATE"
export CURSOR_OPENAI_USD_PER_MTOK_OUT="RATE"
export CURSOR_ANTHROPIC_USD_PER_MTOK_IN="RATE"
export CURSOR_ANTHROPIC_USD_PER_MTOK_OUT="RATE"
```

`run-agent.sh` parses both configuration files as whitelisted `KEY=value`
data. Shell commands, substitutions, and unsupported keys are rejected.

Calibrate a route only after its task-free identity/readiness probe, adapter
contract, and conformance smokes pass. Cursor output is redacted while
streaming; the redacted `.out` artifact remains local and ignored, while the
manifest and ledger carry durable provenance.

The disabled experimental Kimi route uses Claude CLI transport, the OpenRouter
gateway, and Moonshot inference for `moonshotai/kimi-k2.6`. It is in no profile
and has not had a live or billed pilot. Credential rotation is required before
one. File-based injection keeps the token out of argv and durable output, but
without a broker or OS isolation a same-UID process may still observe it.

## Release and activation model

Machine-local release state lives under `~/.factory/kits`:

- `releases/<full-sha>/` contains a verified Git tree with no Git metadata,
  safe symlinks only, and no write bits.
- `manifests/<full-sha>.suite.json` is owner-only, expiring evidence for the
  exact install suite result. It is reusable only for the same sealed release,
  physical tree, host, OS/architecture, suite definition, tool version, and
  configured evidence lifetime.
- `projects/<project>/active.json` is the authoritative per-product release
  record.
- `projects/<project>/activation-journal/` records recoverable activation
  transactions.
- `receipts/` contains mode-`0600`, expiring certification receipts.

The stable `~/.factory/bin/factory-launch` is the Hermes trust root. It parses
the selected `active.json` once, validates the full SHA, tree, contract,
registered product, and exact physical release path, then uses only that
release for the invocation. Contracts `1.0.0` through `1.3.0` expose machine-readable
`contract`, `doctor`, `preflight`, and `next-stage` commands. Contract `1.1.0`
also adds bounded ticket `claim`, `renew`, and `release`. `run` and
`reorder-test-fixes` cross the same launcher boundary but keep process output.
Contract `1.2.0` adds sealed `models`, ticket-state, and ledger controls;
contract `1.3.0` composes them with evidence-bound ticket attestations.
See [hermes-integration.md](hermes-integration.md) for the schemas and commands.

Ticket content is read from the launcher's validated ticket worktree, while
controls and the Linear operator overlay remain anchored to the registered
product root. Linear projection reads the committed exact ticket branch rather
than a dirty checkout. Contract 1.2 ticket routes reject tracked or untracked
worktree dirt before any helper runs. `ticket-state` is the only launcher path that
materializes operator fields or commits a factory-owned role-stage transition.
Contract 1.2 stops in Review after the Narrator posts the bundle. Both generic
transition and operator materialization refuse Awaiting Approval, Approved, and
Done until dedicated trusted bundle and merge/deploy attestation paths exist;
`next-stage` therefore does not authorize `AWAIT-MERGE` under 1.2. Every
automatic helper push is bound to the exact product
origin in the active generation's owner-only certification receipt; mutable
Git remote configuration cannot redirect it.
Contract 1.3 adds only the dedicated `ticket-attest` route. `bundle` binds the
latest successful Reviewer and Narrator runs, reviewed SHA, unchanged
post-review ticket/bundle paths, bundle Git blob, and the unique exact open PR,
then records Awaiting Approval. `approval` consumes only a newer exact Linear
Awaiting Approval → Approved overlay, commits the approval attestation, and
requests normal protected GitHub auto-merge for that exact PR head. `done`
requires the exact merged commit on authoritative `origin/main`, all configured
post-merge contexts successful on that commit, and projects accounting into a
separate closeout branch with a terminal attestation and Done ticket. It never
bypasses protection, force-pushes, or lets the dispatcher manufacture approval.
When concurrency is two, every attestation action also requires the matching
unexpired opaque dispatcher lease through the trusted launcher environment;
the lease is validated with the existing lease helper and never enters an
attestation or command result. Done starts only from `HEAD == origin/main`,
binds the exact approved PR head and protected bundle/approval blobs, and
refuses status/check name collisions. It projects and commits once, then owns
creation/reuse and protected auto-merge of the exact closeout PR. A retry
revalidates the same remote commit. Only valid attested Done on protected main
produces sequencer action `COMPLETE`, after which the dispatcher releases the
lease; closeout PR creation is never terminal evidence.

A one-time Contract 1.2 migration may instead use the separate
`factory/migrations/contract-1.3/` legacy-closeout format. It does not create or
satisfy ordinary bundle, approval, Done, or route-plan attestations. The local
`scripts/legacy-closeout.py` generator reads immutable Git evidence and GitHub
PR/check metadata, requires settled Reviewer/Narrator accounting, and performs
no commit, push, merge, or Linear mutation. Its exact authorization and
complete receipt batch remain inert until the operator manually merges the one
protected product PR containing the receipts, terminal ticket projections,
and target pin. Review sources use `legacy-reviewed`. The pre-four-job
T-013–T-016 PRs alone use `legacy-reviewed-aggregate`: their authentic
app-bound aggregate `ci` and `test-immutability` checks are supplemented by an
independent criteria audit and current combined-test digest. Only the explicitly
bounded Planning anomalies T-019/T-020 may use `out-of-band-merged` with the
same additional audit evidence. Historical tickets explicitly record that
route plans were absent. Activation and sequencing share the same
strict protected-main validator: plain `State: Done`, partial batches, extra
files, conflicting normal attestations, or a receipt targeting another kit
fail closed.

A second one-time format,
`factory/migrations/contract-1.3-terminal-backfill/`, is independently bounded
to the exact authorized pre-contract terminal-Done batch. Its generator records
missing historical bundle and Kit-SHA evidence as null and never synthesizes a
route plan, approval, or check. Authorization binds the product repository,
protected-main basis, target kit, cutoff, immutable current and closeout ticket
blobs, historical implementation/closeout PR metadata and ancestry, ledger
evidence, and the authentic app-owned successful checks available in each
implementation era. It may share the first batch's exact atomic cutover commit;
both validators then require the complete union of files. The complete batch
remains inert until one manual protected product merge.
The shared protected-main reader accepts exactly one of normal attestations,
the first legacy-closeout receipt, or this terminal-backfill receipt; overlap,
partial/extra batches, changed sources, or inconsistent ancestry fail closed.

Overlay-driven state materialization is limited to Backlog-to-Ready and the exact
declared non-sensitive resume from Blocked-Escalated;
factory-owned phases use the transition action. Projection falls back to
committed `HEAD`, never live checkout bytes, when no exact ticket ref exists.

Before the first role, `models pin` resolves one exact six-role plan and records
it with `Kit-SHA:` in one committed and pushed ticket-branch transaction. Every
later preflight, sequencer call, and run refuses a different physical kit SHA;
roles read only their pinned tuple and never re-resolve. A run re-probes only
that exact route. After task submission, failure is terminal and never triggers
retry or fallback. Activation does not migrate pins, so a drained ticket
boundary and a scan of committed exact ticket branches remain required.

`MAX_CONCURRENT_TICKETS` in the product `PROJECT.env` defaults to `1` and may
be set only to `2`. At `2`, every sequencing and role launch requires the
matching opaque record under `factory/.dispatch-leases/`. Claims are atomic,
stale records are never reassigned automatically, and the product-level control
lock serializes complete provider intervals. The global ledger lock remains an
additional serialization and accounting boundary when a machine cap is configured.
Maintenance blocks claims and
renewals while allowing matching owners to release; activation and rollback
refuse until every lease drains. The kill switch clears only validated safe
lease state after stopping recorded runs.

Certification binds the candidate kit SHA/tree/origin, product path/origin/Git
tree, pin and project-config hashes, contract, host, OS/architecture, checks,
previous generation, and expiry. The default receipt lifetime is 24 hours.
Activation reruns those bindings and refuses stale or drifted receipts.
Installation and certification serialize the kit-suite evidence decision under
the install lock. Certification may reuse an unexpired passing suite result for
the exact unchanged sealed release, but always reruns product certification and
all product, config, receipt, and activation validation. Fresh certification
refreshes evidence only after the isolated suite, tracked-tree check, and sealed
release verification pass. Product receipts bind the exact evidence ID/digest
and cannot expire after that evidence.
An activated contract 1.2 or 1.3 keeps that receipt as the runtime destination
binding for trusted ticket and role pushes. Its `product_origin` is the sole
certified `origin` push URL, which may differ from the fetch URL.

Activation uses `factory/MAINTENANCE` and the same launch lock as role startup.
Launch checks occur before locking, after locking, and before the task GO
signal. Maintenance must be published first; activation then waits for the
launch lock, active-run drain, and dispatcher-lease drain. This ordering prevents a new task from
crossing the release switch.

The activation journal advances through `prepared`,
`maintenance_published`, `launch_drained`, `services_stopped`,
`activation_record_switched`, `integration_bundle_switched`,
`services_started`, `healthy`, and `committed`. `reconcile` rolls back
pre-switch interruptions and either validates/commits or restores post-switch
interruptions. The service-named phases are transaction checkpoints; the
current script does not itself manage `launchctl` or perform the external
health smoke.

Rollback restores the previous activation record and sealed tree but keeps
`MAINTENANCE`. The protected product `KIT_PIN` must then be reverted and the
previous tuple revalidated before execution resumes. Automatic pruning is not
implemented; referenced and rollback-eligible releases are retained.

## Role and approval flow

Planner, Builder, and Narrator use the selected portfolio's production family.
Spec-linter, Test-author, and Reviewer use its distinct checking family.
`legacy-balanced-v1` retains the historical OpenAI/Anthropic choices;
`openai-priority-v1`, `claude-priority-v1`, and `cursor-priority-v1` provide
explicit alternative ordering. Narrator converts verified results into the
evidence bundle the operator approves. The exact lifecycle and failure routes
live in [workflows/ticket-flow.md](workflows/ticket-flow.md).

Ticket-plan provenance records catalog/profile/policy hashes and every selected
route tuple. It can support future provider, family, or model budgets, but
those limits are not implemented. The envelope remains the budget authority
and the ledger schema is unchanged.

## Trust boundaries

- Model output is untrusted data: validate it before persistence and never interpolate it into commands, queries, or HTML.
- The wrapper owns budget and timeout enforcement; role prompts cannot weaken it.
- Builders cannot change protected tests; CI checks commit authorship and paths.
- Product credentials stay in GitHub or the hosting platform, never in repositories or agent output.
- External sends require sandboxing or allowlisting, an explicit destination, and irreversible-action evidence.
- External agent frameworks may supply an execution or sandbox transport only behind `factory-launch`. They do not own sequencing, budgets, role selection, Git pushes, ticket state, evidence, or approval; every candidate is pinned and must pass the factory conformance boundary before product use.
- The local plugin AI review is pre-publication hygiene for changes to this kit. It does not replace the factory's independent Reviewer, Narrator bundle, or human approval.
- Factory-owned generic state transitions refuse while operator-owned overlay fields are pending. Contract 1.2 has no trusted bundle-attestation path, so an approval overlay is a stop condition. Contract 1.3 confines Awaiting Approval, Approved, auto-merge, and Done to evidence-validating `ticket-attest` actions.
- Allowlisted machine configuration comes only from `global.env`; inherited values with the same names are cleared even when the file is absent.
