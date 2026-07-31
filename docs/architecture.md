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
Runtime-ledger refresh stages its atomic replacement inside the ignored real
runs directory so concurrent refreshes never create an untracked registered
checkout boundary.

Each ticket-and-role run takes an atomic `mkdir` claim under
`factory/.active-runs/` before it creates a manifest. A conflicting or
abandoned claim always refuses the launch; ordinary launch never guesses that
a PID is stale or reclaims the directory. Cleanup removes only the exact owner
record it created. The wrapper also keeps provider output on an unlinked open
descriptor until the provider exits, then publishes the ignored `.out`
artifact. Missing, malformed, or oversized telemetry cannot reduce spend: a
post-GO run keeps the full reservation and zero turns when its cost data is not
usable.

The provider adapter starts inside a new process group only after a separate
wrapper publishes its PID/PGID and receives the trusted controller's final GO
acknowledgement. The acknowledgement wait is bounded at two minutes because
protected-history validation can exceed ten seconds on migrated products. A
timeout still exits without starting the adapter. Immediately before opening
the gate, the controller rechecks kill, maintenance, and targeted cancellation
state. After observing the gate, the isolated wrapper checks those controls
again before spawning the adapter; that second check is the submission
boundary, and later controls follow normal post-submission drain semantics.
When that boundary stops a launch, integrity checks still run and exempt only
the exact kill, maintenance, or targeted-cancellation record that caused it;
any concurrent manifest, claim, lock, ledger, or checkout mutation still fails.
Cancellation previews are deterministic over the exact manifest, PID record,
and reason so separate plan and apply launcher invocations agree; any mutation
to that snapshot changes the preview hash and refuses the apply.

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

## Dynamic CI selection

`bash ci/test-all.sh` always runs the complete canonical suite registry.
`--changed BASE [HEAD]` classifies a committed diff, while `--shadow-changed`
calculates the same recommendation but executes the full registry.
`CI_FORCE_FULL=1` overrides changed or shadow selection. Metadata-only diffs may skip behavioral
suites because repository, secret, artifact, and immutability gates remain
separate required checks.

Selection fails closed to full for invalid or empty comparisons, additions,
deletions, renames, unknown or shared paths, multiple components, dependencies,
contracts, launchers, roles, CI or selector changes, malformed modes, and empty,
duplicate, or unknown suite IDs. Only explicitly mapped single leaf components
can recommend their direct and transitive suites plus CI-scope, immutability,
and artifact-policy checks. The six audited leaf mappings remain available for
focused local work. Pull requests run the same targeted-or-deferred selection
on Linux and macOS: mapped leaf changes execute their suites, while broad work
runs policy gates and defers complete coverage. Pushes to `main` partition the
complete registry into three named shards per platform so the slow
factory-script, Hermes-contract, and release groups run in parallel. Release
evidence requires all six shard jobs plus the aggregate and immutability jobs
for the exact merged SHA. Historical runs with no shard jobs retain their
legacy two-platform proof for rollback; any partial shard topology fails
closed.

During shadow execution, an unselected failure fails CI and is rerun once. Only
a repeated failure is recorded as `SHADOW_MISS`; a passing recheck is recorded
as a flake. A component may become active only after three real shadowed diffs,
zero reproducible misses, and same-runner median targeted time at most half of
full with at least ten local minutes saved. Its first five active PRs retain a
non-required full comparison. Any miss demotes the component to shadow and
resets its evidence.

Local pre-PR verification uses `--changed-or-defer`. Targeted and metadata
changes complete locally; every broad change, including selector, workflow,
registry, baseline, and policy changes, runs only CI-scope, immutability, and
artifact-policy locally and records full behavioral verification as deferred
to required GitHub CI. The command succeeds when those local policy gates pass.
An explicit argument-free `bash ci/test-all.sh` remains the complete local
command; GitHub `main` divides that same registry across its six shard jobs.

Installation requires remote full-suite evidence for an exact `origin/main`
SHA with a successful authenticated GitHub Actions push run whose three Linux,
three macOS, aggregate, and immutability jobs all passed. It then runs a
sandboxed host smoke check locally. Missing, malformed, or unavailable remote
evidence fails closed without running the complete suite locally. Expired
certification evidence uses the same corroboration and smoke path.

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
preview hash. Without an activation record, `cursor-balanced-v2` is the
default; its OpenAI-production/Anthropic-checking split is profile policy, not
a fixed architectural requirement. Contract 1.4 can append an
operator-approved mid-ticket route revision after an eligible failed attempt;
completed roles remain immutable and contributor-family history constrains
every remaining role. See [model-routing.md](model-routing.md) for the exact
default routes and fallback rules.

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
contract, and conformance smokes pass. Cursor readiness runs in a disposable
owner-only home populated from validated owner-only source files; Cursor never
receives the source home, and the disposable home is removed after the probe.
Cursor output is redacted while streaming; the redacted `.out` artifact
remains local and ignored, while the manifest and ledger carry durable
provenance. Mutating Factory Cursor roles stay in the default agent execution
mode. Reviewer stays in native read-only Ask mode with noninteractive approval
so it can run read-only terminal checks.

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
release for the invocation. Contracts `1.0.0` through `1.8.0` expose machine-readable
`contract`, `doctor`, `preflight`, and `next-stage` commands. Contract `1.1.0`
also adds bounded ticket `claim`, `renew`, and `release`. `run` and
`reorder-test-fixes` cross the same launcher boundary but keep process output.
Certification and every later receipt validation require that installed trust
root to be byte-identical to the candidate release's launcher. A release whose
launcher changed must therefore be explicitly bootstrapped before
certification; activation can never combine a new helper protocol with an old
launcher parser.
Contract `1.2.0` adds sealed `models`, ticket-state, and ledger controls;
contract `1.3.0` composes them with evidence-bound ticket attestations.
Contract `1.4.0` adds route-journal migration and operator-approved mid-ticket
model fallback.
Contract `1.5.0` adds fixed operator snapshots, project-owned model policy,
bounded envelope overrides, targeted attempt cancellation, and the loopback
multi-project console.
Contract `1.6.0` expands bounded coupled capacity to six and defines a
transactional isolated-worker protocol. The provider coordinator serializes
only short SQLite admission and terminalization transactions. Those
transactions enforce machine-day, product-day, and ticket caps in integer
micro-USD using active reservations plus terminal charges. The executor
copies a sanitized source and immutable input into an unprivileged,
digest-pinned ephemeral container and streams only bounded artifacts back.
Worker identity binds ticket, role, attempt, base SHA, route, policy, image,
input, source, and command. The release-owned image lock pins the exact worker
digest. For patch artifacts, the host controller independently checks the
artifact-tree hash, immutable identity, telemetry, base SHA, manifest paths,
protected paths, file modes, and a temporary-index application before applying
and committing the patch under a per-ticket lock. Production activation remains gated on
staged local four-worker canaries and dedicated real-provider API-route
certification. Native subscription/Cursor CLI routes remain on the serialized
legacy path.
Contract `1.7.0` reuses the same transactional coordinator for exact
owner-activated subscription CLI routes. Activation binds the selected CLI
adapter/model/family/account tuple and canonical provider-policy digest; all
limits remain policy-owned. Activation permits account capacity through four
for Codex, native Claude, and Cursor when each attempt has an isolated
owner-only home, configuration directory, temporary directory, and credential
copy. The installed launcher anchors those attempt runtimes in owner-local,
ticket- and execution-cell-neutral state; concurrent Codex, Claude Code, and
Cursor processes never share writable CLI homes. Contract 1.6
and non-activated 1.7 routes keep the
serialized path unchanged.
Its trusted ticket-state reconciliation binds a successful Reviewer's
read-only head and durable output digest, records the canonical verdict and
explicit repair owner, and commits a rejection's Review-to-Building transition
in the same host-owned change.
Contract `1.8.0` replaces the agentic dispatcher and supervisor with a
non-overlapping one-shot controller. `launchd` invokes it every 15 seconds and
watches terminal run evidence for an immediate wakeup. `state-machine` calls
`next-stage` exactly once and issues a one-use receipt bound to the ticket
head/tree, evidence, route, passport, Factory release, and certified origin.
Roles consume that receipt unchanged and never select their next state.
Before scheduling an existing blocked, claimed, or waiting ticket after a
release change, the controller authenticates and migrates its stale passport
and reacquires only that ticket's lease. A waiting ticket therefore cannot
reach stage resolution with a route from the new release and a passport from
the old release. If maintenance appears after stage resolution but before role
submission, the controller records the unconsumed receipt, settles the ticket
for that invocation, parks its clean checkpoint, and releases its lease. The
next invocation issues an ordinary descendant receipt; no provider call,
charge, or successful-role replay is created by the interruption.
The role runner retains the validated project only in a non-exported host
binding for its receipt rechecks; provider processes never inherit the
project's model-state controls. Its trusted exact-head remote observation
retries one failed transport call before classifying branch drift; a second
failure or any different head still fails closed without pushing.
The ticket-PR boundary applies the same one-retry rule only to its exact
read-only branch-head observation; every semantic mismatch and second
transport failure still refuses publication.
Protected-base attestation applies the same one-retry boundary only to its
exact read-only `ls-remote`; mutations and semantic refusals never retry.
Reviewer terminalization accepts the role contract's standalone verdict,
exact verdict-only or `Verdict:` Markdown headings, and exact Markdown-wrapped
repair-owner lines. Cursor background-completion text concatenated to the owner
is split only when every verdict and owner signal remains identical. Ambiguous,
contradictory, or ownerless output is charged but not recorded as completed-role
evidence; the controller reruns only Reviewer under the remaining ticket
budget.
The ticket-budget stage reduces immutable charges against the authenticated
base envelope plus any active ticket-scoped cap override. Budget-wait claims
bind both the envelope and immutable override-record bytes, so adding an
authorized cap reacquires the exact ticket lease and reopens the retained
passport/cell directly; expired, conflicting, malformed, or unrelated records
never raise the effective cap. An authenticated persistent replacement keeps
its predecessor immutable and names that exact record in its preview-bound
hash. It is effective only for the same scope, target, base-envelope identity,
and setting-key set, with a later issue time and no shorter lifetime; missing,
ambiguous, or malformed supersession lineage refuses reduction.
Six-role model-plan pinning relies on its individually bounded readiness
probes and has no aggregate controller timeout; slow successful probes cannot
become a wall-clock delivery stop before the first provider call.
Planner preflight validates the complete pinned route contract without
repeating machine probes; the role runner re-verifies only its selected route
immediately before provider admission.
Automatic qualification fallback serializes and reroutes only the failed
role. Future pinned roles keep their selections and are re-probed only when
they reach provider admission; later task-bearing roles retain four-way
execution.
If interruption occurs after a terminal role was exported but before its claim
was cleared, restart identifies the exact run, role, and transition receipt in
both passport charge and completed-role evidence. It authenticates and
migrates that passport instead of exporting or executing the role again; every
partial or mismatched checkpoint remains fail-closed.
If a prior controller version removed a claim, qualification restart may
reconstruct it only from one signed nonterminal passport, one exact checked-out
branch, current ticket/route Kit-SHAs, and a newly claimed lease. New-admission
refusals stop admission but never prevent already authenticated claims from
reconciling.
Authenticated passports preserve completed roles, charges, Factory/base
lineage, and publication state across disposable-cell relocation, controller
restart, and Factory migration. Four PRs may validate concurrently; one
renewable per-product publication lease serializes merge requests. A ticket
whose terminal boundary spans one or more Factory migrations reuses that
evidence only when one unique contiguous authenticated migration suffix links
the receipt's Factory/head to the exact current Factory/head/base and the
first versioned edge names the receipt-bound passport file digest. A pre-v2
snapshot may cross exactly one new edge only through a one-file protected-main
authorization that binds its receipt, source passport and complete legacy
history, target identity, and terminal accounting evidence. The signed edge
records that authorization's commit, path, blob, and digest; export rereads it
and requires the protected-main snapshot to equal the signed lineage endpoint.
Any later base first needs another authenticated migration edge. The
authorization commit must add exactly its mode-`100644` record and change
nothing else. Broken, ambiguous, unbound, reused, or broadly authorized
lineage fails closed.
A ticket
withdraws its queue record whenever deterministic reconciliation no longer
classifies it as merge-ready; an active lease still requires its exact
capability-bound release. Each controller worker continues its ticket through
deterministic terminal boundaries until that ticket reaches a real wait;
sibling workers do not wait for its checkpoint. A dispatch lease heartbeat
starts before route resolution and provider admission queues. The controller
serializes only protected-base Git mutations because disposable
cells share one Git common directory. Refresh proves staleness from the exact
certified remote tip, ancestry, and exact open PR identity; GitHub's lagging
`mergeStateStatus` is not evidence. The two exact receipt-topology
refusals—an old merge no longer in branch history, or a receipt commit no
longer directly after its merge—route through the same receipt-bound
protected-base refresh. `Building` is admitted only when the trusted launcher
supplies one of those exact stages; the refresh performs the ordinary sealed
reset and never treats stale evidence as valid. The
qualification reducer reconciles passports, manifests, controller events,
protected checks, PR heads, merge commits, and protected main. Its closeout
target is three or four tickets at capacity four. An excluded claim remains
parked and untouched except that startup withdraws any lease-free publication
queue record that could block selected tickets; the reducer still requires the
authenticated four-ticket restart and relocation proof. Historical role,
charge, publication, and merge evidence keeps its original Factory SHA inside
each passport's authenticated release history. Ticket totals use only
currently authenticated ticket-cap overrides, while the cohort remains within
the fixed qualification budget.
Dependency admission accepts ordinary protected terminal truth. A product
upgrading from pre-Contract-1.8 history may instead adopt an already-merged
Backlog dependency through one atomic dependency-fulfillment migration. The
operator-approved batch binds the exact protected basis, product repository,
merged PR heads and merge commits, required successful check identities,
source ticket blobs, and target Factory SHA. It installs that SHA in the same
protected commit without projecting the legacy ticket to Done. Missing,
partial, modified, auto-merged, bypassed, non-Backlog, or ambiguous evidence
remains unresolved; any partial terminal chain fails before this compatibility
path is considered. Runtime dependency checks use only the immutable receipt
after its protected introduction and make no GitHub call.
When a resolved dependency requires the ticket branch to absorb protected
main, the provider-free refresh first attempts the exact non-force merge. A
regular both-modified conflict wholly inside `TEST_PATHS` from the exact
protected `factory/PROJECT.env` is the only automatically recoverable
conflict class. The trusted host records every stage-1/2/3 mode and blob,
retains the protected blob as the safe merge baseline, commits the exact
two-parent merge and a direct-after-merge v2 receipt, migrates the passport,
and has the deterministic state machine create an HMAC-bound
`FIX test-author` checkpoint. Exactly one new Test-author run reconciles the
frozen ticket contract against that protected baseline; earlier successful
roles and charges remain immutable. A sibling merge may advance protected
main while that checkpoint runs; the signed historical base remains valid and
the normal dependency refresh absorbs the newer base afterward. Retirement
requires the exact consumed FIX receipt, authenticated terminal passport,
matching completed-role and charge records, and a diff containing only
regular `100644` modifications to the listed tests or ticket log. The
checkpoint is archived after that unique success. An already-merged
publication closes before dependency refresh is considered. Application,
mixed-owner, Factory-control, contract, CI, configuration, add/delete,
rename, non-regular, unknown, missing-receipt, or tampered conflicts remain
fail-closed.
That atomic introduction may also contain the exact in-flight release
authorization for the same target Factory SHA. No other migration,
application, test, contract, or CI path is admitted.
The sealed qualification launcher binds its owner-only qualification root as
the release environment for isolated subscription runtimes. That root carries
the same trusted marker used by disposable development environments; provider
attempts therefore retain per-attempt homes without depending on a ticket's
cell path. Environment preparation also rejects a root whose worst-case Cursor
attempt data path would exceed the adapter's isolated-scratch limit.
If a proven Factory defect requires a successor during qualification, the
same preparer upgrades that root only while reconciliation and provider work
are drained. It seals the successor, verifies unchanged provider policy,
atomically advances the activation record, and preserves the controller
directory, passport key, passports, claims, and cumulative provider ledger.
After the ticket route migrates, a blocked claim may recover only when its
authenticated passport names the prior release. The controller binds a fresh
exact-ticket lease, migrates that passport in place, and returns the claim to
deterministic reconciliation. A contract blocker remains blocked until the
consumed transition receipt, unique terminal role evidence, passport lineage,
and exact Linear resume state agree. Across a release migration, the state
machine accepts the historical receipt only when the current authenticated
passport orders both releases, contains the exact immutable charge and
manifest digest, has no successful evidence for that receipt, and the old head
is in current branch ancestry. A live current exact-ticket lease is validated
independently; an absent old lease may therefore be replaced without weakening
receipt, terminal, passport, or current ownership checks. If an earlier
controller cleared the blocked claim fields during that migration, the
successor restores them only from the latest passport-bound charge and exact
terminal receipt. A same-release controller restart may also replace an
expired exact-ticket lease after the block was materialized. Replaying that
already-completed block is idempotent only when the authenticated passport
binds the same receipt, charge, role stage, blocked state, resume target, and
receipt-to-passport-to-current-head ancestry; otherwise the rotated lease
cannot authorize the historical transition. An operator appends the first
exact repair-owner and blocked-receipt directive pair, or replaces the one
visible pair for a later blocker, without changing any other path:
`OPERATOR RESUME: <role>` and
`OPERATOR RESUME RECEIPT: <transition-receipt-sha256>`.
The state machine selects the unique receipt-directive commit whose single
parent is an authenticated head in the current passport or its v2 migration
history and whose commit remains in current branch ancestry. The receipt must
equal the exact current consumed blocker receipt. A missing pair, an older
receipt, zero or multiple in-window commits, more than one visible directive,
merge commits, malformed additions or replacements, multi-path changes, or
unrelated head drift fail closed; neither a historical role directive nor a
stale Linear resume state can authorize a later provider call. The state
machine persists an HMAC-bound repair record for the unique pair and runs only
the named owner. If the owner precedes the visible coarse state, that state
remains unchanged while the authenticated repair receipt runs the earlier
role; ordinary deterministic stages then catch up without adding a general
backward state transition. After catch-up, the signed completed-repair archive
authenticates the still-visible role-and-receipt pair so it cannot be mistaken
for a new repair. More than one successful owner run fails closed.
After an authenticated resume creates that repair record, the controller may
observe either a receipt-free blocked claim or the unchanged failed-role
receipt and role left by the terminal blocker. The latter is eligible only
when both fields exactly match the repair record's blocked receipt and role.
The deterministic state machine then authenticates the signed repair and
resolves its owner before the controller clears those stale claim fields.
Mismatched, partial, unauthenticated, or active-role claims remain blocked.
An unresolved dependency may temporarily replace the visible transition
receipt without discarding that repair record. After the dependency and any
Factory upgrade resolve, the named owner reopens only when the record's signed
passport is the unique start of a contiguous authenticated v2 migration suffix
and the original failed charge remains unique and unsuccessful.
If that migrated owner then succeeds, the suffix ends at the consumed repair
receipt's role-input head rather than the new role-output head. The state
machine accepts the advance only when the current authenticated passport, one
terminal manifest, one completed-role record, and one charge all bind that
same receipt, role input, Factory release, run, output digest, and current
descendant head. The terminal passport must name the exact input-passport file
digest carried by the receipt, and the charge must contain a canonical
nonnegative micro-USD amount. If that terminal passport is itself migrated
before the repair is retired, one unique second v2 suffix may bridge its
descendant output head to the current Factory. The first post-success edge
must preserve the pre-success Factory, protected base, and route identity; the
suffix must remain contiguous through the exact current Factory, head, base,
and route, and its final source-passport digests must equal the current
passport's parent digests. Missing, duplicate, disconnected, or altered
evidence remains fail closed and never causes the successful role to be
replayed.
When a zero-provider qualification attempt leaves only canonical pin and state
commits on a now-divergent remote ticket branch, a protected-main reset
authorization may bind its exact head. Admission validates that no ticket
contract or product path changed, non-force merges current protected main,
removes only the obsolete pin/state control, and preserves the old commits in
branch history before repinning. Every unlisted head or non-control change
fails closed. A later successor may repeat that recovery only when every
first-parent commit follows the same canonical pin, transition, merge,
supersede, and repin grammar and every merged base belongs to current protected
main lineage.
During a protected qualification, the development scheduler authenticates a
durable Contract 1.7 Planner, Test-author, or Builder contract-blocked result
against the exact qualification kit SHA before returning only that ticket to
`Backlog`. Outside qualification it retains the ordinary
`Blocked-Escalated` workflow. In either case its lease is released and sibling
lifecycles continue. A qualification Spec-linter `FAIL` also returns the
ticket directly to Backlog instead of entering the ordinary replan/round-three
authorization loop.
The first terminal failed Cursor attempt for a protected qualification keeps
its claim and authenticated evidence while the controller appends the existing
same-family direct-CLI fallback and resumes the same deterministic stage. The
fallback atomically converts an initial v1 route plan into a same-release v2
journal before appending its revision, preserving the original plan bytes and
provenance. It is idempotent across controller restart. A second task-submitted
attempt for that ticket and role is refused as no progress instead of replayed.
An exit 143 before task submission reopens only that interrupted role, and only
after the current signed passport, clean cell, branch, and remote head agree
exactly; every submitted or differently terminated interruption stays blocked.
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
If protected main advances after review, `refresh` first disables any stale
auto-merge request, non-force merges the exact certified main tip, removes the
old bundle and approval receipts, resets the ticket to Review, and commits a
receipt binding the two-parent merge and prior role/verdict baselines. The
sequencer requires a new Reviewer verdict and later Narrator run unless the
receipt's immutable base delta contains only modified regular blobs at exact
`factory/KIT_PIN` and `factory/QUALIFICATION.json`, plus added regular blobs at
exact `factory/migrations/inflight-release/<40-hex>.json` paths. In that narrow
case the attestor also requires the retained ticket-head blobs to equal the
protected base and every preserved Reviewer/Narrator manifest head to be an
ancestor of the receipt-bound old head. Earlier historical rows from a
discarded force-pushed lineage remain auditable; the latest effective Reviewer
and the later effective Narrator decide reuse. An orphaned latest Reviewer
reruns Reviewer and downstream Narrator, while a missing or orphaned Narrator
reruns only Narrator. Every application, test, contract, CI, configuration,
rename, type, deletion, and unknown-path change invalidates review. A malformed
or stale refresh receipt refuses sequencing.
The early ticket-PR boundary applies that same decision instead of comparing
only commit SHAs. It accepts retained control paths only under Contract 1.8,
an exact receipt-authorized stage, a committed direct-after-merge refresh
receipt, unambiguous merge ancestry, the shared non-semantic classifier, and
control blobs that still equal the receipt's protected base. Route-journal
changes retain their independent append-only migration validation.
When concurrency is greater than one, every attestation action also requires the matching
unexpired opaque dispatcher lease through the trusted launcher environment;
the lease is validated with the existing lease helper and never enters an
attestation or command result. Done starts only from `HEAD == origin/main`,
binds the exact approved PR head and protected bundle/approval blobs, and
refuses status/check name collisions. It projects and commits once, then owns
creation/reuse and protected auto-merge of the exact closeout PR. A retry
revalidates the same remote commit. Only valid attested Done on protected main
produces sequencer action `COMPLETE`, after which the dispatcher releases the
lease; closeout PR creation is never terminal evidence.

Before bundle attestation, the sequencer checks the Narrator artifact for the
same required sections and approval question as the trusted attestation path.
When that artifact is structurally invalid and no bundle attestation exists,
exactly one additional Narrator run is allowed; another invalid result
escalates instead of entering operator approval.

The Done receipt always binds the hash of the complete projected ledger. Its
closeout commit includes `factory/ledger.csv` only when projection changes the
tracked bytes; a prior concurrent closeout may already have projected the same
terminal run set. Ticket and Done-attestation files remain mandatory in every
closeout commit, and no other paths are allowed. Protected terminal validation
checks that hash at the immutable closeout commit and requires the current
ledger to retain those bytes as an unchanged prefix, allowing only later rows
to be appended.

A control-plane release may close an already-approved ticket from its older
ticket-pinned release. Done validates the protected bundle and approval against
that canonical ticket `Kit-SHA` and records the same SHA in its receipt; legacy
ticket files without the field retain their already-protected bundle SHA. It
does not reinterpret prior role evidence as belonging to the newly active
release. Bundle and approval actions still require the active physical release
exactly.

One authentic Contract 1.2 `primary_ready` Planner manifest may coexist with a
later fully pinned Planner run. It remains legacy accounting evidence rather
than route-plan authority: every historical invariant and shared route field
must match, and its exact manifest digest is bound in a v2 bundle receipt.
Normal tickets continue to emit and validate v1 receipts; every broader legacy
shape is refused.

An exceptional in-flight release cutover requires a protected-main
`factory/migrations/inflight-release/<target-kit-sha>.json` authorization. Its
v1 schema binds the product `repository`, one `source_kit_sha`, the exact
`target_kit_sha`, and a sorted nonempty `tickets` list whose entries contain
only `ticket`, exact ticket `branch`, remote `head`, and current `state`.
Activation accepts only Ready, Planning, Building, Review, Awaiting Approval,
Approved, or Blocked-Escalated remote ticket heads that match every authorized
field, and refuses an unused or extra entry. Blocked-Escalated is admitted only
as the exact preserved ticket state; activation and route migration do not
resume it or reinterpret its evidence. Every exact head must also contain
either a v1 ticket route plan or a v2 route journal whose ticket and Kit-SHA
match the authorization and whose complete history passes the candidate's
migration validator. The normal maintenance, zero-active-run, and
zero-dispatcher-lease barriers still apply.
Qualification upgrades bind liveness to the non-overlapping controller lock
and active-run markers. A terminal orphaned `running` claim remains portable
state for the successor controller rather than an upgrade deadlock.
Upgrade recovery keeps the claim blocked while it first authenticates the
current clean head into the successor passport. This pre-route boundary lets an
exact protected rewrite authorization bind the unchanged old route digest.
The state machine never migrates a passport for a `REFUSE` transition; the
controller blocks the claim first so the next one-shot owns that boundary and
its durable pending marker.
After the preview-bound route migration commits the successor Kit-SHA to both
the ticket and route journal, a second ordinary descendant migration updates
the passport route digest and only then reopens the claim. A durable
controller marker makes the between-migrations restart boundary idempotent.
If an operator-authorized repair deliberately removes invalid Git ancestry, a
cross-release passport migration may use that protected authorization only
when its source/target kits, ticket, branch, new head, and state match exactly,
the cell is clean, and the authenticated route digest is unchanged.
A same-release Test-author repair uses a narrower protected authorization under
`factory/migrations/ticket-rewrite/<new-head>.json`. It additionally binds the
old signed passport, consumed `FIX test-author` receipt, typed failed non-force
push, old/new heads, unchanged route, and exact repository. Migration requires
a clean cell whose final-tree delta contains at least one configured test path
and no path outside configured tests plus the exact ticket log; only ordinary
added or modified regular blobs are accepted. The failed attempt's immutable
charge is folded into the migrated passport, but it is not promoted to
successful role evidence. The controller never force-pushes: it waits until
the exact operator-authorized head is the remote ticket tip, then reopens only
Test-author through the ordinary state machine. Missing, stale, dirty,
non-Test-author, semantic, route-changing, or differently headed rewrites
remain blocked.
After activation, the operator uses the existing preview-hash-bound `models
migrate` flow. A v1 plan becomes a v2 journal; an existing v2 journal receives
one parent-hashed release-migration revision that preserves every prior
revision and the active resolution. Both paths update the ticket Kit-SHA before
work is reclaimed. The initial v1-to-v2 schema-migration revision may preserve
the same Kit-SHA; only a later `release-migration` revision claims a release
change. In both cases the embedded legacy bytes, digest, policy, selections,
pin commit, and revision hash chain must match exactly. This authorization
never changes ticket state, migrates a branch, renews a lease, or permits an
unprotected-main record.

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
roles read only their active journal resolution. A run re-probes only that exact
route and never silently retries a task-bearing process. Contract 1.4 may
migrate the v1 plan and append a fallback revision only after an eligible
terminal GO attempt, one-use Linear approval, validated partial-work snapshot,
and full family-history resolution. Activation does not migrate pins or
journals automatically. Contract 1.8 resolves machine readiness once before
concurrent ticket execution, then pins one to four clean ticket branches from
that in-process batch resolution. This prevents both duplicate probes and
concurrent release-integrity fan-out from starving bounded CLI startup; role
execution and protected PR validation remain four-ticket concurrent. A
readiness plan containing only ready and temporarily unavailable routes yields
one shared controller wait for the cohort; it does not release claims or block
tickets. Invalid or unknown route evidence still fails closed.

`MAX_CONCURRENT_TICKETS` in the product `PROJECT.env` defaults to `1` for
Contracts 1.1 through 1.5 and `4` for Contracts 1.6 through 1.8. The older
contracts accept integers through `4`; Contracts 1.6 and 1.7 accept `1`
through `6`, while Contract 1.8 accepts `1` through `4`. At any value above `1`, every sequencing and role launch
requires the matching opaque record under
`factory/.dispatch-leases/`. Claims are atomic and deterministically refuse once
the configured capacity is full. Stale records continue to consume capacity and
are never reassigned automatically. This is the single coupled ticket-worktree
and provider-call capacity setting. The product-level control lock still
serializes older contracts, explicit single-ticket products, and every
non-activated legacy provider interval. Contract 1.8 with capacity above one
instead requires owner-only canonical subscription activation, policy,
coordinator state, and per-attempt runtime roots covering every enabled Cursor,
Claude Code, and Codex route. Doctor, certification, activation, and role
pre-admission all refuse when that configuration is absent, incomplete,
drifted, or below ticket capacity; they never silently serialize a multi-ticket
release. Contract 1.6 and Contract 1.8 capacity one retain the fail-closed
legacy path. Only an exact API route selected by the owner-only Contract 1.6
activation file may use the API-isolated runtime.
The legacy global ledger remains an additional serialization and accounting
boundary when a machine cap is configured.
Contract 1.6 workers have no network and never receive provider credentials or
broker tokens. The owner-local credential broker keeps credentials in a
mode-0600 configuration and exchanges an attempt token for credentials only
while the trusted runtime proxies an exact approved HTTPS path. The runtime
relays only the bounded provider result into the worker. Tokens bind the
attempt, route, model, reserved budget, expiry, and bounded request count;
revocation, expiry, wrong-model requests, redirects, and unconfigured
destinations fail closed. Only token-free, credential-free state is exposed by
status reporting. A broker-stage failure or controller signal releases its
slot only after the token is revoked and the broker reports no request in
flight; otherwise the full reservation remains active. The executor likewise
publishes a successful result only after removing its bound container.
Maintenance blocks claims and
renewals while allowing matching owners to release; activation and rollback
refuse until every lease drains. The kill switch clears only validated safe
lease state after stopping recorded runs.
Renewal serializes only on the dispatcher lease lock, not the provider launch
lock; matching ownership and pre/post mutation control checks keep maintenance
and kill fail-closed while unrelated provider entry cannot starve a heartbeat.

The Contract 1.6/1.7 Hermes supervisor is deliberately one-shot: one invocation
asks the stable launcher for one deterministic claim, starts at most one
ephemeral dispatcher child on `START`, and exits on `WAIT` or `ESCALATE`.
Autonomous claiming requires configured capacity above one so the lease can be
transferred in memory to that child. At the first sequencer-authorized Reviewer
boundary, the trusted ticket-PR helper creates or reuses exactly one open PR for
the clean pushed ticket head. It waits without launching a role while required
checks are not yet reported or pending, supplies completed failures to
Reviewer, and revalidates successful exact-head checks and Reviewer lineage
before Narrator. A later
Builder or Test-author run forces fresh review. The helper has no approval or
merge authority.
Under Contract 1.8, compatibility `dispatch-plan` performs deterministic
admission only and cannot spawn a dispatcher. The release-owned controller is
the sole caller that advances work through state-machine receipts. Concurrent
admission wakeups serialize on a process-scoped lock in the generation-wide
worktree coordinator, not the product launch lock. Candidate and dependency
resolution therefore occurs before the product launch boundary. A selected
claim then revalidates the clean registered checkout, unchanged Linear map,
control markers, live capacity, and exact ticket identity while holding the
launch and dispatcher-lease locks. Slow or empty admission scans cannot starve
an independent role launch or lease release.

Certification binds the candidate kit SHA/tree/origin, product path/origin/Git
tree, pin and project-config hashes, contract, host, OS/architecture, checks,
previous generation, and expiry. The default receipt lifetime is 24 hours.
For a Contract 1.8 product whose ticket capacity exceeds one, certification
and every receipt revalidation also require owner-local subscription
concurrency to cover Cursor, Claude Code, and Codex at no less than that
capacity.
Activation reruns those bindings and refuses stale or drifted receipts.
Installation and certification serialize the kit-suite evidence decision under
the install lock. Certification may reuse an unexpired passing suite result for
the exact unchanged sealed release, but always reruns product certification and
all product, config, receipt, and activation validation. Fresh certification
refreshes evidence only after the isolated suite, tracked-tree check, and sealed
release verification pass. Product receipts bind the exact evidence ID/digest
and cannot expire after that evidence. Products may opt into the sealed
`certification-runner.py` with a repository-owned declarative DAG. It records
wall time, CPU, peak memory, cache status, exact input digests, and artifact
digests for every phase; runs at most three workers; gives each phase a
separate log and temporary directory; and cancels sibling process groups after
the first failure. A passing measured result is bound to the exact Factory SHA
and product tree and embedded in the certification receipt. Existing opaque
certification scripts remain compatible. Cache hits are recorded but the
initial runner does not reuse build or test results; evidence must justify any
future cache policy. Exact protected Factory CI proof remains reused rather
than repeated during product certification.
An activated contract 1.2, 1.3, or 1.4 keeps that receipt as the runtime destination
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
`cursor-balanced-v2` is the no-record default; `balanced-v2` and
`legacy-balanced-v1` remain available for compatibility with prior activation
records and migrations.
`balanced-v2`, `openai-priority-v1`, `claude-priority-v1`, and
`cursor-priority-v1` provide explicit alternative ordering. Narrator converts verified results into the
evidence bundle the operator approves. The exact lifecycle and failure routes
live in [workflows/ticket-flow.md](workflows/ticket-flow.md); exact model
priority and fallback behavior lives in [model-routing.md](model-routing.md).

Ticket-plan provenance records catalog/profile/policy hashes and every selected
route tuple. It can support future provider, family, or model budgets, but
those limits are not implemented. The envelope remains the budget authority
and the ledger schema is unchanged.

## Trust boundaries

- Model output is untrusted data: validate it before persistence and never interpolate it into commands, queries, or HTML.
- The wrapper owns budget and timeout enforcement; role prompts cannot weaken it.
- Cursor treats the configured role timeout as an inactivity window extended
  only by normalized structured events from the trusted stream parser. An
  absolute limit at twice that duration remains nonextendable; malformed,
  rewritten, or unsafe progress evidence fails closed. Other adapters retain
  their configured hard timeout.
- Builders cannot change protected tests; CI checks commit authorship and paths.
- Product credentials stay in GitHub or the hosting platform, never in repositories or agent output.
- External sends require sandboxing or allowlisting, an explicit destination, and irreversible-action evidence.
- External agent frameworks may supply an execution or sandbox transport only behind `factory-launch`. They do not own sequencing, budgets, role selection, Git pushes, ticket state, evidence, or approval; every candidate is pinned and must pass the factory conformance boundary before product use.
- The local plugin AI review is pre-publication hygiene for changes to this kit. It does not replace the factory's independent Reviewer, Narrator bundle, or human approval.
- Factory-owned generic state transitions refuse while operator-owned overlay fields are pending. Contract 1.2 has no trusted bundle-attestation path, so an approval overlay is a stop condition. Contracts 1.3 and 1.4 confine Awaiting Approval, Approved, auto-merge, and Done to evidence-validating `ticket-attest` actions.
- Allowlisted machine configuration comes only from `global.env`; inherited values with the same names are cleared even when the file is absent.
