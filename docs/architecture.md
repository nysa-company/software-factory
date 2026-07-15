# Architecture

## Engine model

The kit is installed as immutable exact-SHA releases and shared by every product. Product repositories contain only their own state. Every live run separates the selected physical release (`KIT_DIR`) from `FACTORY_ROOT` (the product repository's tickets, ledger, envelope, and configuration):

```bash
~/.factory/bin/factory-launch <project> run \
  --role <role> --ticket <T-NNN> \
  --prompt-file <release-role-path> [--workdir <worktree>] -- "task text"
```

- **Kit:** scripts, adapters and version pins, role contracts, workflows, runbooks, and CI templates. Fixes land through reviewed PRs, but a merge does not activate them.
- **Product repository:** `factory/` state (including initiatives and tickets), product documentation, instantiated CI, GitHub rules, and deploy credentials. All products share the Software Factory Linear team; each initiative gets a Linear Project.
- **`factory/KIT_PIN`:** exactly one lowercase, full 40-character certified kit SHA. External products fail closed when it is missing, malformed, or different from the physical release.
- **`factory/PROJECT.env`:** product name, repository slug, protected test paths, worktree location, and ticket branch prefix.

Per-product limits live in each product's `ENVELOPE.env`; the machine limit in `~/.factory/global.env` caps aggregate spend.

Runtime accounting is immutable per run: `factory/runs/<run_id>.meta` records the reservation, durable pre-GO marker, terminal state, cost, and basis. The ignored `factory/runtime-ledger.csv` is a deterministic effective view over those manifests and tracked `factory/ledger.csv`; only launcher command `project-ledger` writes the tracked ledger from a clean `chore/tNNN-closeout` worktree.

The in-repository `conformance/` product is the only implicit-pin exception. It
must share the kit repository, Git common directory, and HEAD. This exception
exists for CI only. Live/external products and deployment certification require
an explicit `KIT_PIN`.

Backend policy is kit-owned and certified by the same `KIT_PIN`. Production roles require the OpenAI family (`codex`, with optional `cursor-openai` fallback); checking roles require the Anthropic family (`claude-code`, with optional `cursor-anthropic` fallback). Fallback is resolved before task submission and is never a retry: one logical role run submits its task to at most one agent process. Machine-specific Cursor model IDs, the approved CLI compatibility version, and `FACTORY_CURSOR_FALLBACK_ENABLED` live in `~/.factory/global.env`. Exact model IDs must be present in `scripts/lib/cursor-model-families.txt`; `auto` is forbidden.

```bash
# ~/.factory/global.env — no credentials in this file
export FACTORY_CURSOR_FALLBACK_ENABLED=0
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

Enable fallback only after `agent status --format json`, `agent models`, `scripts/adapters/contract-test.sh --routes`, and both conformance smokes pass. Cursor output is redacted while streaming; the redacted `.out` artifact remains local and ignored, while the manifest and ledger carry durable provenance.

## Release and activation model

Machine-local release state lives under `~/.factory/kits`:

- `releases/<full-sha>/` contains a verified Git tree with no Git metadata,
  safe symlinks only, and no write bits.
- `projects/<project>/active.json` is the authoritative per-product release
  record.
- `projects/<project>/activation-journal/` records recoverable activation
  transactions.
- `receipts/` contains mode-`0600`, expiring certification receipts.

The stable `~/.factory/bin/factory-launch` is the Hermes trust root. It parses
the selected `active.json` once, validates the full SHA, tree, contract,
registered product, and exact physical release path, then uses only that
release for the invocation. Contracts `1.0.0` and `1.1.0` expose machine-readable
`contract`, `doctor`, `preflight`, and `next-stage` commands. Contract `1.1.0`
also adds bounded ticket `claim`, `renew`, and `release`. `run` and
`reorder-test-fixes` cross the same launcher boundary but keep process output.
See [hermes-integration.md](hermes-integration.md) for the schemas and commands.

Ticket content is read from the launcher's validated ticket worktree, while
controls and the Linear operator overlay remain anchored to the registered
product root. Linear projection reads the committed exact ticket branch rather
than a dirty checkout. `ticket-state` is the only launcher path that
materializes operator fields or commits a factory-owned stage transition.

The first role launch records a `Kit-SHA:` lease on the canonical ticket while
holding `factory/.launch.lock`. Every later preflight, sequencer call, and run
refuses a different physical kit SHA. Activation does not migrate leases, so a
drained ticket boundary and an operator check for conflicting nonterminal
leases remain required.

`MAX_CONCURRENT_TICKETS` in the product `PROJECT.env` defaults to `1` and may
be set only to `2`. At `2`, every sequencing and role launch requires the
matching opaque record under `factory/.dispatch-leases/`. Claims are atomic,
stale records are never reassigned automatically, and the global ledger lock
continues to serialize budget reservations. Maintenance, activation, rollback,
and the kill switch drain all dispatcher leases.

Certification binds the candidate kit SHA/tree/origin, product path/origin/Git
tree, pin and project-config hashes, contract, host, OS/architecture, checks,
previous generation, and expiry. The default receipt lifetime is 24 hours.
Activation reruns those bindings and refuses stale or drifted receipts.

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

Planner, Builder, and Narrator use the production model family. Spec-linter, Test-author, and Reviewer use a separate checking family. Narrator converts verified results into the evidence bundle the operator approves. The exact lifecycle and failure routes live in [workflows/ticket-flow.md](workflows/ticket-flow.md).

## Trust boundaries

- Model output is untrusted data: validate it before persistence and never interpolate it into commands, queries, or HTML.
- The wrapper owns budget and timeout enforcement; role prompts cannot weaken it.
- Builders cannot change protected tests; CI checks commit authorship and paths.
- Product credentials stay in GitHub or the hosting platform, never in repositories or agent output.
- External sends require sandboxing or allowlisting, an explicit destination, and irreversible-action evidence.
- The local plugin AI review is pre-publication hygiene for changes to this kit. It does not replace the factory's independent Reviewer, Narrator bundle, or human approval.
