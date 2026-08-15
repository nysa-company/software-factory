# Architecture

## Engine model

The kit is installed as immutable exact-SHA releases and shared by every product. Product repositories contain only their own state. Every live run separates the selected physical release (`KIT_DIR`) from `FACTORY_ROOT` (the product repository's tickets, ledger, envelope, and configuration):

```bash
~/.factory/bin/factory-launch <project> run \
  --role <role> --ticket <T-NNN> \
  --prompt-file <release-role-path> --workdir <ticket-worktree> -- "task text"
```

- **Kit:** scripts, adapters and version pins, role contracts, workflows, runbooks, and CI templates. Fixes land through reviewed PRs, but a merge does not activate them.
- **Product repository:** `factory/` state (including initiatives and tickets), product documentation, instantiated CI, GitHub rules, and deploy credentials. There is no external board; every initiative is its own `factory/initiatives/I-NNN.md` file.
- **`factory/KIT_PIN`:** exactly one lowercase, full 40-character kit SHA. Production requires a protected-main, successful-CI installed release; a sealed qualification may instead bind one clean local candidate SHA/tree. External products fail closed when the pin is missing, malformed, or different from the physical release.
- **`factory/PROJECT.env`:** product name, exact `GH_REPO`, protected test paths, worktree location, ticket branch prefix, contract-1.3 `DONE_REQUIRED_CHECKS` (a unique comma-separated list of exact post-merge status/check names), required `AUTO_MERGE_METHOD` (`squash`, `merge`, or `rebase`), optional repository-contained `PREVIEW_PREFLIGHT_SCRIPT`, and optional fail-closed `NONVISUAL_PATHS` directory prefixes.

Per-product limits live in each product's `ENVELOPE.env`; the machine limit in `~/.factory/global.env` caps aggregate spend.

Runtime accounting is immutable per run: `factory/runs/<run_id>.meta` records the reservation, durable pre-GO marker, terminal state, cost, and basis. Preflight and the first normal reconciliation durably initialize the ignored `factory/runs/` root before reducing accounting. The reducer opens that root without following symlinks and accepts only regular, single-link manifests. For backward readability, a legacy durable reservation immediately followed by its terminal row reduces to the terminal row; other duplicate run IDs still fail closed. The ignored `factory/runtime-ledger.csv` is a deterministic effective view over those manifests and tracked `factory/ledger.csv`; only launcher command `project-ledger` writes the tracked ledger from a clean `chore/tNNN-closeout` worktree after every product run is terminal and accounted. Projection refuses any active or ambiguous claim under `factory/.active-runs/` and any `factory/runs/*.pid` record; operators must reconcile those records before close-out rather than guess whether a process is stale.
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

Product CI treats an exact `factory/QUALIFICATION.json`-only change as a
lightweight control change only after a separate mandatory gate checks its
committed blob. The gate reads the exact committed `factory/KIT_PIN`, checks
out that private Factory revision with non-persisted read-only credentials,
and invokes the release's shared strict qualification-manifest parser. Local
readiness invokes that same parser through the copied lightweight classifier
from the installed exact-pin release; validator refusal is distinct from the
ordinary broad-test classification.
Unrelated diffs do not fetch the Factory or parse qualification state; any
present malformed or cross-pin manifest fails before product merge without
running a broad application suite.

Selection fails closed to full for invalid or empty comparisons, additions,
deletions, renames, unknown or shared paths, multiple components, dependencies,
contracts, launchers, roles, CI or selector changes, malformed modes, and empty,
duplicate, or unknown suite IDs. Only explicitly mapped single leaf components
can recommend their direct and transitive suites plus CI-scope, immutability,
and artifact-policy checks. The six audited leaf mappings remain available for
focused local work. Pull requests run the same targeted-or-deferred selection
on Linux and macOS: mapped leaf changes execute their suites, while broad work
runs policy gates and defers complete coverage. Repository policy — baseline,
secret scan, and artifact check — runs once per run in its own job and gates
the aggregate `ci` context rather than repeating inside every platform group.
Pushes to `main` partition the
complete registry into four stable groups per platform on separate hosted
runners. Their public suite IDs remain intact. Group membership carries no
meaning beyond balance: each group anchors one slow lifecycle suite plus
filler, sized from observed hosted run durations and rebalanced when a group
drifts. The factory-script suite uses
six fixed internal workers with private temporary roots; lifecycle cases that
share launch, cancellation, Git, accounting, or cleanup state remain
sequential inside one worker. Worker process groups are drained on failure or
interruption, and successful logs are replayed in stable order. The
Factory-contract and factory-kit lifecycle suites remain sequential inside
their own groups. A group is directly
runnable as `bash ci/test-all.sh --group N`; an optional `--shard SHARD` narrows
that group for local diagnosis. After all four groups succeed, three stable
evidence aliases per platform retain the installed release contract. Release
evidence requires all six aliases plus the aggregate and immutability jobs
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
command and runs its four isolated groups concurrently; GitHub `main` divides
that same registry across eight group jobs and retains the six established
release-evidence aliases.
The checked group mapping assigns every registry entry exactly once and new
unclassified suites fail safely into release group 4. Local argument-free and
GitHub runs share the same grouping; an explicit whole-shard command remains
sequential for diagnosis. Suites within each group keep their existing order,
and fixtures remain group-isolated.

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
preview hash. Without an activation record, `cursor-opus-v1` is the
default; its OpenAI-production/Anthropic-checking split is profile policy, not
a fixed architectural requirement. Contract 1.4 can append an
operator-approved mid-ticket route revision after an eligible failed attempt;
completed roles remain immutable and contributor-family history constrains
every remaining role. See [model-routing.md](model-routing.md) for the exact
default routes and fallback rules.

Fallback approval and qualification fallback reduce effective accounting from
the tracked durable ledger and owner-only terminal manifests on every check.
They do not trust `runtime-ledger.csv`, because that file is an ignored output
view and linked production/qualification worktrees may materialize different
copies without changing the underlying accounting evidence.
Where a publication boundary must pair a completed role manifest with its
effective ledger row, both views must come from the same runtime lane. Ordinary
production resolves the ignored ledger beside the claimed ticket worktree's
canonical main checkout. A sealed qualification launcher instead supplies an
explicit trusted override inside its isolated control product, so production
sync cannot rewrite qualification evidence from a different run-manifest root.
Ticket execution, PR preparation, and ticket attestation consume that same
launcher-bound path. Qualification stage selection refreshes that projection
from its own manifest root before consuming it. The code and evidence reducer
are identical; only the lane's mutable runtime file is isolated.

Failed-attempt handoff snapshots cover the Git tree, index, and non-ignored
untracked files. Git-ignored dependency/build trees are outside that snapshot;
every tracked or non-ignored symlink, hardlink, special file, nested repository,
unsafe mode, or unsafe parent path remains forbidden.
Historical committed-role validation may carry an unchanged symlink from its
authenticated baseline, but any added, removed, or changed symlink still fails.

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
The sealed Cursor inventory uses that same disposable-home boundary and first
requires the exact configured CLI identity in an empty disposable home, before
copying credentials. Captured output is owner-only and accepted only after a
stable bounded read. It accepts only the certified `Available
models` envelope, known current/default flags, bounded safe display labels, and
unique selection IDs, returning the IDs rather than terminal presentation
text. Unknown structure, malformed or oversized output, and secret-like values
fail closed without exposing the captured bytes.
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
- `certification-artifacts/` contains the owner-only authenticated, expiring
  artifact entries explicitly admitted by certification plans. The product
  sandbox never receives this persistent path or its authentication key.

The stable `~/.factory/bin/factory-launch` is the Factory trust root. It parses
the selected `active.json` once, validates the full SHA, tree, contract,
registered product, and exact physical release path, then uses only that
release for the invocation. Contract `2.0.0` exposes machine-readable
`contract`, `doctor`, `preflight`, and `next-stage` commands. Contract `1.1.0`
also adds bounded ticket `claim`, `renew`, and `release`. `run` and
`reorder-test-fixes` cross the same launcher boundary but keep process output.
Contract 1.8 additionally exposes `ticket-control pause|resume`: pause requires
one exact Software Factory issue URL, removes only one idle passport-bound
claim, releases its lease, and records an owner-only repro intent bound to its
Factory SHA, head, passport, run snapshot, lifecycle state, Resume-State, and
claim status. An idle `missing-terminal` claim with a canonical receipt,
released lease, known role, no active role, and no terminal evidence is also a
valid issue-bound pause; the run snapshot makes a later terminal publication
invalidate resume rather than replay the role. The signed checkpoint retains
only idle `claimed`, `waiting`,
`blocked`, or `budget` status; a budget pause also binds the exact budget
digest. Resume-State is required for `Blocked-Escalated`; other lifecycle
states retain either JSON null or an allowed existing resume overlay. Resume requires the
operator to name the exact active Factory
SHA, validates the state, passport lineage, remote branch, unique worktree,
recorded claim status, and target release before reacquiring one lease, then
archives the repro record. Backlog, canceled, merged, and Done tickets are
never pause/resume targets. A v2 pause may cross exactly one direct, pushed,
Factory-authored route-migration child that changes only its ticket and route
journal. Resume binds that child as the expected passport-migration head,
rechecks the signed pause, authenticated passport lineage, remote identity,
lifecycle and run snapshot, and remains retryable if capacity acquisition
fails after migration. Legacy v1 pauses, arbitrary descendants, extra paths,
wrong authors, or remote drift are not widened. Startup and
interrupted-reconciliation recovery never turn paused or historical repro
records into runnable claims.
Contract 1.8 also exposes a channel-neutral `watch --json` read boundary over
the selected project's canonical controller events. It projects only bounded,
redacted operator actions for contract or lifecycle escalation, approval,
terminal role failure, terminal recovery refusal or abandonment, budget halt,
Spec-linter round-three authorization, malformed authorization correction, and
progress timeout. The semantic projections are fixed, structured actions: one
asks for the literal canonical grant, while the other names only a closed
append, amend, push, branch, or remote-topology correction. Intermediate
recovery failures remain silent. Terminal
recovery actions expose only closed recovery kinds and bounded reason codes or
the terminal attempt count and authenticated input and outcome digests; raw
recovery errors never enter the projection. Every output retains the
authenticated source-event digest and an
opaque cursor bound to the exact project controller path, source filename, and
digest. Restart validates that anchor and resumes after it; missing, replaced,
reordered, broadly writable, or digest-invalid evidence exits through the same
typed nonzero boundary.
An otherwise valid action whose historical Factory identity is unavailable is
instead emitted under the separate operator-watch-diagnostic/v1 schema with a
fixed reason, null Factory SHA, and no source value echoed; other malformed
action context remains fatal. One process inventories
historical filenames once, then parses only newly published events. The single
live controller serializes event publication and seeds its next monotonic timestamp
from the largest filename on restart without parsing event bodies; files are
fsynced and atomically renamed before becoming visible. The watcher is
read-only, receives no GitHub or provider credential, and provides no delivery
hook; Slack, desktop, or other notification channels may consume its NDJSON
without entering the Factory trust boundary. Production and qualification use
their already distinct launcher-selected controller state paths.
Installed Contract 1.8 production Doctor also checks the exact managed macOS
LaunchAgent, its native disabled-service override, and its label-specific
`launchctl list` dictionary. A live PID or an idle job whose latest exit is
zero is `ok`; an explicit disabled override, missing job, route mismatch,
malformed native response, or idle nonzero exit is `error`. Absence from the
override map is the native default-enabled state. The bounded
`checks.controller` projection contains only `status`, closed-set `state`, and
nullable `last_exit_status`; it never echoes labels, paths, arguments, logs,
environment, or native command output. Qualification, disposable lanes,
non-macOS hosts, and older contracts report `not_applicable` without querying
launchd. This check complements durable controller events; watcher silence is
not controller-liveness evidence.
Doctor's event reducers retain one narrower compatibility boundary for
authenticated production history: exact legacy `contract_blocker_recovered`
records with a null Factory identity and one bounded `failed_run_id`, exact
`ticket_released` records with a null Factory identity, and exact legacy
`upgraded_claim_recovered` records with either a null Factory identity or a
self-referential `from_factory_sha`, are ignored. They never resolve or hide a
contract-resume or transition-receipt incident. Digest, schema, observation
time, and ticket are validated first; every incident-bearing null identity and
every broader legacy shape remains an error.
At controller startup, actionable durable claims are reconciled against one
inventory of canonical events. A crash-lost budget, approval, known block,
pre-provider failure, or terminal role failure is republished once only when
its current-release transition and, where applicable, HMAC-authenticated
passport and exact terminal evidence agree. This closes the mutation-to-event
crash window without a second event journal or per-ticket history scans.
The product test-immutability gate treats one ticket-only higher numbered
frozen contract plus its matching PASS marker as a new tests-first epoch. New
Planner output uses the canonical append-only marker. Historical Planner output
may replace only the latest heading and its matching established PASS marker
one-for-one; a partial, mismatched, repeated, lower, mixed, or malformed change
does not reset the gate. Git keeps the earlier role input immutable while the
new epoch reopens Test-author ownership. Same-contract late tests may use the
reorder helper on a clean local tail. It never moves a commit across a merge,
preserves every retained two-parent merge's exact tree and second parent,
refuses octopus merges, and moves the branch only after final-tree identity
succeeds. The helper never pushes. An already accepted remote history may move
only through the separate protected normalization authorization and an
explicit exact-head force-with-lease.
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
Production and qualification retain separate provider lifecycle and financial
databases. A task-bearing Cursor role additionally acquires one owner-only
machine-local account-route lease before durable GO and holds it until its
process group drains. Lanes sharing an account route must present identical
concurrency and start-window limits; disagreement refuses, distinct account
routes remain independent, and production waiters precede qualification for
the final slot. Readiness probes and route planning never enter this admission
path or write the shared database.
For an active product with a certified Node/npm tuple, each isolated CLI home
also carries a final zsh login hook. It restores the launcher's exact task PATH
after macOS `path_helper` runs and exits before the requested product command
when either runtime version differs; the provider CLI's own bundled runtime is
outside this check.
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
Cached transition receipts are never re-stamped during that migration. Their
digest and stable identity are checked first; only an exact current-release
receipt is returned as transition authority. A valid prior-release receipt or
an invalid receipt yields no authority, emits one ticket-scoped typed event,
and is excluded from ordinary recovery, admission, and scheduling for that
sweep. An inactive invalid claim releases its lease without rewriting the
receipt; a live role is untouched. Existing typed release-upgrade, terminal,
and contract-blocker recovery may still use a digest-valid prior receipt under
their stricter bindings. One roleless prior-release maintenance refusal may
also re-enter ordinary scheduling after its exact protected in-flight release
and route migration is complete. Admission requires the authenticated current
Review passport, one contiguous release/head/base/route suffix from the
receipt-bound passport file, the current ticket lease and route, the exact
clean remote head, and either the current reconciliation marker or its
authenticated unchanged-run successor. The ordinary state machine then issues
and persists the current receipt as a parent-linked child before the controller
removes the prior-receipt exclusion; the recovery grants no direct role,
provider, or publication authority. The one unmerged
stale-bundle recovery may ask the
state machine for a current receipt only after one unique contiguous suffix of
authenticated release-changing migration edges binds the old receipt and
passport-file digest to the current passport without changing head or route.
The old receipt retains its historical lease while the successor receipt binds
the controller's current active lease. A release-scoped marker is durable
before issuance, so restart
reuses the exact current receipt; its digest stays out of the claim's pending
role field and authorizes only the launcher's existing `ticket-attest refresh`
boundary. Done and Canceled claims retire before this check,
while Doctor reports the latest unresolved receipt incident per ticket.
Transition receipts and ticket passports are authenticated over the exact
newline-terminated canonical JSON document bytes emitted by their writers.
The controller does not accept an alternate encoding or re-stamp either
artifact.
The role runner retains the validated project only in a non-exported host
binding for its receipt rechecks; provider processes never inherit the
project's model-state controls. Its trusted exact-head remote observation
retries one failed transport call before classifying branch drift; a second
failure or any different head still fails closed without pushing.
After a verified push, remote-tracking projection remains compare-and-swap:
an already-converged desired SHA is idempotent success, and an explicitly
expected absence initializes the ref through Git's zero-OID compare-and-swap.
Every third, unexpectedly missing, or unreadable ref state fails closed.
The ticket-PR boundary applies the same one-retry rule only to its exact
read-only branch-head observation; every semantic mismatch and second
transport failure still refuses publication.
Protected-base attestation applies the same one-retry boundary only to its
exact read-only `ls-remote`; mutations and semantic refusals never retry.
Reviewer terminalization accepts the role contract's standalone verdict and
exact verdict-only or `Verdict:` lines with a bounded Markdown heading,
emphasis wrapper, or terminal period, plus exact Markdown-wrapped repair-owner
lines. Cursor background-completion text concatenated to the owner is split
only when every verdict and owner signal remains identical. Ambiguous,
contradictory, negated, prose-only, or ownerless output is charged but not
recorded as completed-role evidence; the controller reruns only Reviewer under
the remaining ticket budget.
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
Contract 1.8 successor qualification keeps its sealed $100 ticket cap and
reserves the final two $10 run slots for exact-current-head Reviewer and
Narrator revalidation after a semantic protected-base refresh. The committed
refresh v2 receipt binds the candidate Factory and carries one reservation
generation across later refresh generations of that same candidate. Ordinary
roles stop prospectively at $80, refresh Reviewer at $90, and refresh Narrator
at $100; a failed Reviewer therefore cannot consume the Narrator slot through
a second paid retry. Legacy, malformed, foreign-Factory, or absent receipts
cannot spend the reserve, and ordinary envelope overrides do not change it. A
successor Factory receives a fresh fixed allowance because its authenticated
accounting is candidate-scoped.
Six-role model-plan pinning relies on its individually bounded readiness
probes and has no aggregate controller timeout; slow successful probes cannot
become a wall-clock delivery stop before the first provider call.
State-machine reconciliation likewise relies on its individually bounded
resolver, ticket-state, passport, and Git operations. The controller does not
apply a second aggregate timeout that could terminate the parent after a
ticket-state transition has already been committed.
Independent successor passport migrations overlap up to the already-certified
ticket capacity. Each ticket still crosses the same launcher, passport, route,
lease, and accounting validators; only the prior cross-ticket serialization is
removed.
The focused deterministic suite enumerates every `RUN` and `FIX` role from
every lifecycle state with mocked role work. It asserts the exact forward or
repair path and the forbidden backward edges in seconds, before a sealed lane
spends time on a real provider task.
Before interpreting any transition, the controller validates the complete
state-machine envelope: schema, status, ticket, action, detail, receipt digest,
typed stage, and exact stage-to-role mapping. Any mutation blocks and releases
the lease before provider or publication work.
Operator authority is expressed entirely through one-use receipts (`scripts/lib/operator_receipt.py`, issued by `scripts/operator-cli.py`), anchored in the controller's state directory. There is no external system to reconcile, poll, or retry against, so there is no scheduled cycle, quota cooldown, or comment/description sync to describe: `factory/operator-map.json` is computed on demand from committed ticket state and consumed receipts, each map mutation owns only a short map lock, and staleness has no meaning for a pure projection. During ordinary admission, malformed dependency syntax is isolated to that exact ticket and reported in controller results, events, and incident evidence while eligible siblings continue. The same defect in a selected qualification ticket remains globally fail-closed for the sealed cohort. A missing operator initiative remains authoritative, but if it removes a Ready ticket's effective initiative, admission emits a named `initiative_missing` refusal while eligible siblings continue. Initiative assignment is a direct ticket-only Git commit setting `Initiative:` — there is no external Project object to mark, adopt, or reconcile.
Qualification preparation applies the same operator lifecycle to its fixed cohort: a selected Backlog ticket receives a one-use Ready receipt and local ticket-branch materialization, while an already-durable Ready ticket needs only projection initialization. Admission accepts the normal consumed-and-cleared projection after materialization but still rejects malformed or uninitialized entries. Because the reducer proves protected GitHub publication truth, preparation binds the certified push origin to the exact repository declared by `factory/PROJECT.env` and refuses a local-only origin before it publishes lane state. After bounded historical-object hydration, preparation also requires every dependency outside the cohort to satisfy the shared protected dependency predicate: normal terminal evidence or one exact protected dependency-fulfillment receipt. Later independently authorized batches live under an immutable directory named by their target Factory SHA; the original flat batch remains valid and unchanged. An isolated lane is never published with a dependency that dispatch cannot honor.
Qualification role sequencing treats the ticket at the sealed product SHA as
the immutable prior epoch. One shared read-only projection requires every
parser-recognized spec, review, void, and semantic-authorization control line
from that baseline to remain an exact ordered prefix, then exposes only the
appended suffix to current role accounting. Ticket bytes and global Reviewer
round numbering are preserved; production has no projection and keeps its
ordinary whole-ticket semantics.
Preparation also inspects only the selected remote ticket branches before it
creates runtime or authority state. An absent branch or one descending from
protected main is ready; a divergent branch requires its exact protected reset
authorization and must pass the same canonical control-history validator that
dispatch rechecks immediately before its exact-head CAS reset. A v2 reset may
name an earlier qualification generation only when it binds that generation's
Factory and product SHA and the exact branch head; the accepted delta is the
durable Ready base or canonical Ready receipt, one route pin, and ticket-only
qualification work.
Any application path or source mismatch remains fail-closed. The narrower v1
pre-provider grammar is unchanged.
Reset replay accepts only the exact canonical merge, its exact ticket/control
cleanup, or the resulting main-tree supersede commit. After a pushed
operator-ready reset, dispatch reissues Ready through the normal one-use
operator receipt and binds materialization to that exact reset SHA; an advanced
remote is refused rather than adopted.
A later qualification generation may reauthorize that exact rematerialized
Ready head. Its validator recursively proves each earlier operator-ready epoch,
canonical reset merge, main-tree supersede, and later Ready suffix before the
new reset; no earlier control history is skipped or trusted from authorization
alone.
Automatic GitHub defect reporting is an optional production sidecar, never a
controller dependency. The controller marks only explicit internal invariant
failures with a stable reason code; the reporter accepts a fixed allowlist of
those codes, publishes only bounded identity metadata, and deduplicates by a
reason-code fingerprint. Unknown, product, operator, budget, and external
failures remain local evidence for ordinary triage. GitHub failure leaves the
event pending for the next reporter run and cannot change a claim, lease,
passport, ticket state, or controller result.
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
Every pre-provider worker submission also records a passport-, head-, route-,
run-snapshot-, and release-bound reconciliation marker. A restart may reopen a
receipt-free blocked claim only when that marker and every current invariant
still match; typed blocks, pauses, dirty or diverged cells, terminal evidence,
active runs, and unauthenticated cross-release claims remain closed. If the
interrupted worker already wrote one or more authenticated passport successors,
the marker may advance only across the unique contiguous v2 suffix from its
exact release, head, and passport digest to the current passport, with aligned
release history, an unchanged run snapshot, clean cell, and exact current
remote passport. A durable authorization event precedes the atomic advance, so
either crash edge replays idempotently.
Conflicting evidence is persisted as a ticket-local block before best-effort
resource cleanup and cannot prevent healthy siblings from reconciling. The
marker is consumed after ordinary worker completion, making recovery one-use
and idempotent.
An idle parked current-release `state-machine-refusal` may also re-enter only
after authenticated protected main changes from the base bound by its refusal
receipt, or by the receipt-bound passport for legacy receipts. The controller
requires the unchanged roleless claim, receipt inputs, current route and
passport, clean exact branch, current tracking ref, and exact remote passport,
then asks the ordinary state machine for a different parent-linked transition.
Only that accepted result updates the claim; an unchanged refusal or malformed,
dirty, foreign, paused, active, or remote-divergent input remains blocked. A
ticket-local prepared marker precedes temporary lease acquisition. On restart,
that exact marker adopts only the ticket's dispatcher lease and its parent-linked
child receipt, so crashes on either side of the state-machine write are
idempotent. The lease is released on refusal, and a cleanup failure is persisted
on that same claim so it cannot consume sibling capacity invisibly. If later
validation invalidates a prepared attempt, its exact ticket lease is likewise
released, or persisted on the blocked claim for ordinary inactive cleanup,
before the marker is retired. Marker reconciliation precedes cancellation,
completion, and claim-status filtering; failed cleanup therefore retains a
fail-closed claim instead of orphaning capacity when those filters run.
Authenticated passports preserve completed roles, charges, Factory/base
lineage, and publication state across disposable-cell relocation, controller
restart, and Factory migration. Four PRs may validate concurrently; one
renewable per-product publication lease serializes merge requests. A ticket
with an authenticated current passport sequences from that passport's ordered
completed-role evidence, not from only the manifests still present under the
current release's product checkout. The state machine validates the passport's
ticket, branch, head, route, release, and HMAC before passing an owner-only
ephemeral role sequence to its single `next-stage` invocation. The ignored
runtime ledger remains accounting truth and remains the sequencing source for
a new ticket that has no passport. This prevents release migration or
qualification takeover from replaying an already preserved Planner,
Spec-linter, or other successful role merely because its historical manifest
is no longer present in the current checkout.
Takeover preparation computes that authenticated completed-role closure from
each authorized passport and retains only its exact mode-600 manifests,
outputs, and progress journals under the owner-only controller root. It
restores those bytes into the ignored qualification run directory before
admission, and the controller repeats the same digest check immediately before
every role dispatch. When completed evidence has one authenticated correction,
retention keeps the immutable failed terminal and accepts only the exact typed
exit authorized by that correction; its progress identity comes from the same
signed record. Missing, changed, unsafe, ambiguous, uncorrected, or mismatched
evidence blocks before a provider starts.
A sealed qualification scopes provider product- and ticket-budget admission to
the exact project and frozen candidate SHA. Different qualification roots for
that same candidate share the scope, while predecessor-candidate charges stay
outside its allowance. Provider lifecycle and financial accounting stay
lane-local; task-bearing Cursor runs alone also obey the machine-local
account-route concurrency and start-window admission described above.
Before hydration or runtime publication, fresh isolated preparation validates
the product's existing envelope against the sealed qualification manifest:
every effective role reservation fits the per-run cap, the ticket cap is exact,
and both the product and supplied machine daily caps cover the manifest budget.
A mismatch refuses before any claim or provider work instead of producing a
qualification that can only fail later at admission or reduction. Takeover
continues to use its separately authenticated live-state budget contract.
Its release and CLI scratch remain disposable under `/private/tmp`, but signed
passports, controller events, provider accounting, paused worktrees, and HMAC
authority live under the owner-only `~/.factory/qualification/<project>` root.
Restore accepts only a fully paused, drained environment whose Factory/product
tuple, manifest, runtime tuple, passport, branch/head, stage, pause digest, and
run snapshot still match; it rebuilds no authority from Git history.
Before provider admission, qualification also hydrates only immutable PR refs
named by committed terminal migrations, verifies their exact expected heads,
and leaves every checked-out tree and ref unchanged. Migration and attestation
inputs, aggregate parsed bytes, object counts and sizes, and staged fetch bytes
are bounded; protected-main evidence must name exactly `refs/heads/main`.
Preparation creates an absent ignored `factory/runs/` through the
owner-controlled physical `factory/` descriptor at mode 0700; a file, symlink,
foreign owner, or permissive runtime directory refuses. Every selected
unfinished ticket must also use exact `Product-Decisions: frozen` metadata.
A cohort containing one selected ticket that depends on another is rejected in
favor of sequential generations. Fresh isolated preparation requires one
explicit owner-only canonical operator map seed. It authenticates the seed's
path, mode, structure, and digest, rejects secret-bearing fields, and copies it
once to `~/.factory/qualification/<project>/operator/operator-map.json` before
initializing exactly the selected tickets. The mutable map, reconciliation
locks and clear intents, and runtime ledger stay under that operator directory;
the receipt and active record bind their exact paths, and the sealed launcher
exports them on every qualification command. Preparation rechecks the product
worktree after initialization and publishes no usable environment if it is
dirty. An owner-only bootstrap receipt lets a retry reuse a partially
initialized lane map even if the original seed later changes or disappears,
so exact-ticket adoption cannot create duplicates.
Qualification preparation is serialized per project and classifies the
owner-local state as fresh, exact-incomplete, or exact-complete. Every retry
reruns live fallback readiness, then creates or byte-validates the sealed
release, pristine provider state, receipt, authority, activation, environment,
and final `active.json` record in that order. Release materialization uses a
same-directory temporary tree and rename; `environment.json` precedes the
active record so a lost final response is replayable. Only an exact pristine prefix
or complete lost-response state resumes. A changed artifact, missing
predecessor, materialization remnant, active controller/provider, or unexpected
entry refuses without deletion. This does not widen the signed safe-pause
`--restore` boundary and adds no cleanup authority.
The pre-publication controller prefix may retain only the exact owner-only
locks and consumed Ready receipts created while materializing the selected
Backlog cohort. Those zero-authority audit records are validated on replay;
claims, passports, runs, other receipts, or malformed entries still refuse.
Initial create or adoption
uses one bounded exact-title query. Before creation it records a ticket-, team-,
Project-, and title-bound uncertain intent; a returned ID is persisted before
observation. Restart therefore fetches that exact ID or waits to adopt one exact
Factory issue in the intended Project and never repeats an uncertain create.
Confirmation requires its complete canonical non-canceled state. Only that
exact observed issue clears the intent and records selected-ticket success. A matching
one-use clear is consumed for that ticket only; sibling issues and clears are
not read or changed. The create mutation itself never retries an ambiguous
transport or quota response. Historical tickets are not reconciled.
Production-successor takeover continues to bind the canonical live map instead
of copying it.

After a successful Reviewer publication commit, passport migration precedes
cell parking so the expected validating-head change remains a waiting boundary.
After terminal or protected-cancellation retirement removes a durable claim,
the next reconciliation holds the dispatch admission lock and removes only a
clean exact `cell-1..cell-6` worktree with no remaining claim, dispatch lease,
or active run. Dirty, claimed, leased, active, foreign-branch, and ambiguous
cells remain untouched; Git branches and remote refs are never deleted. The
current controllers derive the exact product or qualification worktree root
from the existing authenticated release path identity; new launcher
revisions never add controller arguments that a sealed active release may not
parse. Reclamation therefore cannot search or mutate unrelated worktrees
without breaking cross-release launcher compatibility.
Contract 1.8 certification requires exactly one `PREVIEW_PROVIDER`. A `none`
provider also requires a strict nonempty `NONVISUAL_PATHS` policy, and every
selected ticket must declare exact Builder-owned files wholly inside it before
qualification publishes state. Ambiguity or deployable work therefore stops
before a claim, lease, or provider attempt.
Narrator admission additionally requires every Railway preview deployment
linked by the trusted bot comment to report the exact reviewed repository,
branch, and commit; stale or unavailable identity waits without a role charge
and becomes one typed timeout. The sealed `ticket-control retry-preview` action
may restart only that exact expired wait against its unchanged pushed head,
receipt, passport, and released lease; it resets no evidence and ordinary
reconciliation still requires the preview identity to pass. A product-configured preview preflight runs only
after that identity passes. Its bounded JSON result must bind the same head;
`wait` remains uncharged, `fail` blocks before Narrator, and missing, unsafe, or
malformed evidence refuses. Products without the optional hook retain exact-SHA
deployment admission without an invented topology policy. The only no-deploy
path is an explicit certified `NONVISUAL_PATHS` policy whose complete GitHub PR
file inventory contains added or modified semantic files solely under those
non-overlapping directory prefixes and otherwise only exact current-ticket
Factory metadata. The helper binds that decision to the reviewed head and file
set digest, and Narrator still produces the ordinary approval bundle with
Preview and Screenshots explicitly not applicable. Absence of the policy,
mixed or empty semantics, unknown Factory paths, removals, renames, copies, or
malformed GitHub evidence retains the Railway requirement or refuses. A merged terminal
closeout persists one exact passport/PR/protected-main/ticket-blob request
before the idempotent Done action. Only that request may reopen a clean parked
`controller-error`; unrelated errors remain blocked. Later protected-main
advancement remains eligible only when the recorded base and both bound merge
commits are ancestors and the protected ticket blob is byte-identical.
A ticket
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
reset and never treats stale evidence as valid. The qualification reducer
reconciles passports, manifests, controller events, protected checks, PR heads,
merge commits, and protected main. A qualification may close either an
explicitly ordered three-ticket cohort at capacity three or four independent
tickets at capacity four. Tracked dependencies must form an acyclic graph and
every dependency outside the cohort must already have protected terminal
evidence. The three-ticket form proves the exact selected restart, lifecycle,
and serialized publication but makes no PR-concurrency claim; whenever any
target still needs candidate publication it also requires one exact cell
relocation. An all-terminal successor accepts no relocation only when every
target is already covered by protected reconciliation, emergency
reconciliation, or authenticated terminal adoption; it never reopens work to
synthesize one. The four-ticket form additionally requires overlapping PR
validation. An earlier
fresh three-ticket qualification may retain its authenticated four-ticket
restart boundary; a production-successor cohort requires the exact selected
three-ticket boundary. An excluded claim remains parked and untouched except that
startup withdraws any lease-free publication queue record that could block
selected tickets. Historical role, charge, publication, and merge evidence
keeps its original Factory SHA inside each passport's authenticated release
history. A fresh cohort retains the $2 run, $25 ticket, and $100 cohort
envelope. A production successor binds the installed source Factory SHA,
reuses canonical controller passports and provider accounting in place, and
limits only new candidate spend to $10 per run, $100 per ticket, and $300 for
the cohort. Its reducer requires an authenticated source-to-candidate passport
migration, validates historical and candidate charges for duplication, and
reports cumulative and candidate-only totals separately. It never copies or
re-signs live passports.
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
If the dependency becomes terminal after publication evidence exists, the
same exact dependency receipt routes through the ordinary publication refresh
instead. It verifies the receipt-bound protected tip and dependency truth,
makes the exact PR draft, retires the bound bundle and approval blobs, resets
the ticket to Review, and records the existing `ticket-refresh/v1` receipt.
Reviewer and Narrator must establish fresh evidence over the changed semantic
base. A moved protected tip returns to waiting without changing PR or evidence.
A controller restart after the refresh push recognizes only the exact committed
refresh receipt, retired evidence diff, remote branch, and draft PR, then
continues the same post-push reduction without another merge or push. The
sealed `dependency-refresh-replay` action requires the ticket's exact dispatcher
lease; the committed refresh receipt replaces, rather than reuses, the consumed
transition receipt as replay authority.
That atomic introduction may also contain the exact in-flight release
authorization for the same target Factory SHA. No other migration,
application, test, contract, or CI path is admitted.
The sealed qualification launcher binds its owner-only qualification root as
the release environment for isolated subscription runtimes. That root carries
the same trusted marker used by disposable development environments; provider
attempts therefore retain per-attempt homes without depending on a ticket's
cell path. Environment preparation also rejects a root whose worst-case Cursor
attempt data path would exceed the adapter's isolated-scratch limit.
Its immutable registered product checkout may be detached at the exact
authenticated protected-main SHA; role admission records that identity while
ticket execution remains branch-bound.
For a production-successor qualification, preparation additionally requires
a clean linked qualification worktree of the canonical product repository,
the selected tickets' authenticated passports with an exact release-history
suffix rooted at the installed source, or the source cohort's exact
digest-bound protected-terminal reconciliation when that terminal ticket has
no passport, a drained controller,
and the current production provider activation. The activated source checkout
must be clean and match its authenticated product tree, while current protected
main must contain that source commit. This permits shared control policy to
advance without activating it first. The qualification worktree must be based
on current protected main and may differ only by the candidate pin, successor
manifest, and dependency lines of selected tickets; this local authority does
not require a setup pull request. The sealed helper environment binds the exact
qualification product tree authenticated by the owner-only qualification
activation record. Preflight therefore requires that clean tree in this one
environment;
the terminal exception requires the identical source-manifest cohort, exact
controller event boundary, zero charge, unchanged protected ticket and Done
blobs, and current protected-terminal validation. A passport's successful
role may pair with conservative accounting only when the retained run manifest
and output still prove completed submitted execution, exit zero, role success,
and an exact full-reservation charge. Adjacent same-source passport migrations
may bridge a changed head only through a complete linear chain of those
authenticated successful roles whose commits satisfy the sealed per-role path
policy; a missing, cancelled, foreign, failed, merged, or bare Git edge refuses.
An ordinary or unbound launcher continues to require a clean, current `main`
checkout. The helper also binds the canonical live operator map and revalidates
it on every launch, so ticket-state
logic consumes the same approval overlay as production. Before sealing, every
selected ticket blob in that control tree must equal the protected
`origin/main` blob used by dispatch. Dispatch repeats the comparison after its
authoritative fetch and before selection, lease creation, or worktree
materialization; a missing or changed blob refuses without copying local
control changes into a ticket branch. The sealed launcher keeps its candidate
release and disposable worktrees under the qualification root while resolving
controller state, route state, and provider accounting to the canonical owner-only roots. The
installed and sealed launchers therefore contend on the same reconciliation
lock; once protected product main names the candidate pin, the installed old
launcher also fails closed on that pin. This is one authoritative ticket
history, not copied sandbox state. The mandatory controller restart boundary
and its recovered proof are keyed by the frozen candidate SHA and validate the
exact qualification ticket set; a marker from an earlier candidate cannot
satisfy either boundary. Successor runtime ticket budgets count only terminal
charges whose Factory SHA matches that frozen candidate, exactly like the final
reducer; authenticated earlier-candidate charges remain in cumulative passport
accounting but cannot consume the new candidate's allowance. A cross-release
successor may reopen that prior candidate's budget wait, while same-release and
ordinary production budget waits remain closed. A takeover admits one frozen
candidate. If that candidate exposes a semantic defect after authenticating its migrations,
the replacement uses a new qualification root while retaining the installed
production source in the manifest; preparation and final reduction require the
unique contiguous v2 release suffix through every intermediate candidate.
For takeover qualification the authenticated product worktree remains outside
the sealed root, so candidate-scoped provider admission validates the
launcher-supplied sealed lane root directly rather than requiring the product
to be nested beneath it.
If a proven Factory defect requires a successor during an isolated fresh
qualification, the same preparer upgrades that root only while reconciliation and provider work
are drained. It seals the successor, verifies unchanged provider policy,
atomically advances the activation record, and preserves the controller
directory, passport key, passports, claims, and cumulative provider ledger.
Before that upgrade publishes any successor state, every selected ticket must
already have an authenticated passport whose release history is rooted at the
successor manifest's source Factory. A missing, malformed, foreign, mixed, or
candidate-native passport refuses the upgrade and instructs the operator to
start a fresh ordinary qualification; neither the controller nor reducer may
infer source history from work performed after the candidate starts. The
passport's complete migration chain and retained role accounting must agree
with that authenticated release history. Before activation, candidate or
foreign charge, completion, and correction evidence is not source work and
refuses the upgrade; an already-active candidate may validate its own exact
lineage evidence on an idempotent restart.
If the defect instead requires a fresh successor root after an unconsumed
Planner receipt but before any passport or provider run, the predecessor cell
is handed off rather than recreated. Both lane activations, owner authorities,
cohort manifests, controller and dispatch locks, old claim and lease, receipt,
route, pushed head, origin, and protected successor reset authorization must
agree exactly. An owner-only digest journal in the successor controller binds
both roots and records a prefix of `git worktree move` operations. A restart
accepts only the journaled destination prefix, at most one move completed just
before its journal update, and the untouched source suffix. Reverse moves,
both/neither paths, target runtime, source publication or run evidence, and
ordinary outside-root dispatch all fail closed.
After the ticket route migrates, a blocked claim may recover only when its
authenticated passport names the prior release. The controller binds a fresh
exact-ticket lease, migrates that passport in place, and returns the claim to
deterministic reconciliation. A contract blocker remains blocked until the
consumed transition receipt, unique terminal role evidence, passport lineage,
and exact ticket-recorded resume state agree. Across a release migration, the state
machine accepts the historical receipt only when the current authenticated
passport orders both releases, contains the exact immutable charge and
manifest digest, has no successful evidence for that receipt, and the old head
is in current branch ancestry or reaches it through exactly one authenticated
same-release rewrite edge whose old and new Git trees are byte-identical. The
edge must bind the passport's current route, protected base, and Factory; a
missing authorization, changed tree, or ambiguous match remains blocked. A
terminal passport export uses the same contiguous v2 lineage proof before the
controller may invoke block recovery; ordinary exports still require raw Git
ancestry. A live current exact-ticket lease is validated
independently; an absent old lease may therefore be replaced without weakening
receipt, terminal, passport, or current ownership checks.
When a pushed operator edge is ahead of the authenticated blocked passport,
the state machine validates the exact context-only commit or complete
context-plus-resume chain before passport migration. Invalid receipt binding,
ancestry, content, paths, or directives therefore leaves the prior recoverable
passport head unchanged. The sealed migration binds the head returned by that
check and rechecks the same clean identity, ticket state, route, and protected
base immediately before its atomic write. After a crash, the authenticated last
migration edge reconstructs the exact validation; a current remote passport
backfills the migration event once, while an answer-only wait remains
uncharged. Ordinary passport migration and resume still provide the durable
commit points.
If a prior candidate instead left an expired lease file while the migrated
successor claim is parked
and lease-free, the controller first authenticates the exact current passport,
route, branch, and remote head. The sealed lease helper then rechecks and
removes only that expired lease under the ordinary launch/lease locks before a
fresh claim. A renewal race, live run, malformed record, duplicate identity,
wrong ticket, or sibling lease remains closed and untouched. If an earlier
controller cleared the blocked claim fields during that migration, the
successor restores them only from the latest passport-bound charge and exact
terminal receipt. A same-release controller restart may also replace an
expired exact-ticket lease after the block was materialized. Replaying that
already-completed block is idempotent only when the authenticated passport
binds the same receipt, charge, role stage, blocked state, resume target, and
receipt-to-passport-to-current-head ancestry; otherwise the rotated lease
cannot authorize the historical transition. A consumed `FIX <role>` blocker
may retain a coarse resume state later than that role only when the exact
receipt and authenticated blocked passport name the same FIX stage. Block
recovery and resume validate that evidence directly instead of requiring the
repair record that resume has not created yet. When receipt and passport
Factory SHAs differ, the state machine additionally requires the authenticated
historical blocker charge, ordered release history, exact current passport
digest, and receipt-to-current-head lineage before accepting that later coarse
state. Once a signed repair exists, its narrower contract-repair migration
proof remains authoritative.
An operator appends the first
exact repair-owner and blocked-receipt directive pair, or replaces the one
visible pair for a later blocker, without changing any other path:
`OPERATOR RESUME: <role>` and
`OPERATOR RESUME RECEIPT: <transition-receipt-sha256>`.
The ticket records the blocked-state timestamp once per substantive blocker. Exact
resume directive lines do not create a new blocker identity, and later
non-operator writes cannot advance that baseline. A validated operator
decision for the same blocker wins; a rejected move remains visible on the
ticket log instead of being silently patched back.
The state machine selects the unique receipt-directive commit whose single
parent is an authenticated head in the current passport or its v2 migration
history, whose resulting ticket contains the exact visible role-and-receipt
pair, and whose commit remains in current branch ancestry. An authenticated
receipt-withdrawal commit is not an authorization candidate. The receipt must
equal the exact current consumed blocker receipt. A missing pair, an older
receipt, zero or multiple actual in-window authorizations, more than one
visible directive, merge commits, malformed additions or replacements,
multi-path changes, or unrelated head drift fail closed; neither a historical
role directive nor a stale ticket-recorded resume state can authorize a later
provider call. Candidate discovery follows commits that changed either directive line,
so an otherwise exact ticket-only role correction may preserve the receipt.
During idempotent block recovery, that one validated commit may be the
direct child of the authenticated passport head: repair validation stays bound
to the passport head, then the ordinary passport migration authenticates the
directive commit before resume. The state machine persists an HMAC-bound
repair record for the unique
pair and runs only the named owner. If the owner precedes the visible coarse
state, that state remains unchanged while the authenticated repair receipt
runs the earlier role; ordinary deterministic stages then catch up without
adding a general backward state transition. When a completed Planner repair
opens a new test-first epoch beneath Building or Review, its signed archive
retains that same narrow authority through the alternating Planner/Spec-linter
prefix until Test-author or the three-failure loop cap. A post-Reviewer
Spec-linter FAIL may therefore produce `RUN planner` beneath Building or
Review. Receipt verification rechecks the current rejection, authenticated
role prefix, exact branch/head/passport identity, and uncapped
`planner-spec-linter` loop before deriving the launcher-only `CATCHUP planner`
preflight admission. Ordinary `RUN planner`, missing or malformed loop data,
an impossible role order, a stale receipt, and the capped third failure remain
closed. No ticket state is rewound and the receipt schema is unchanged.
If that exact backward repair blocks again, the active signed repair also
authorizes the block at the unchanged coarse state. The block records that
coarse state as its resume target, and the same signed repair must authenticate
both an idempotent block recovery and the later resume; ordinary role/state
drift remains refused.
If a successor is sealed after the backward repair owner commits that blocker,
the active repair remains valid only when the consumed `FIX` receipt, unique
contract-block terminal manifest, authenticated charge, current passport
stage, and contiguous migration suffix all bind the repair head to the
successor head. A descendant migration edge alone is never sufficient.
When that exact consumed repair blocker crosses an authenticated normalized
history, the repair reuses the blocker lineage verifier instead of treating
later contiguous same-release edges as independent starts. Its receipt,
charge, terminal, role, stage, and failed-role evidence remain mandatory;
ordinary repair migrations retain their unique direct-start rule.
After catch-up, the signed completed-repair archive
authenticates the still-visible role-and-receipt pair so it cannot be mistaken
for a new repair. More than one successful owner run fails closed.
After an authenticated resume creates that repair record, the controller may
observe either a receipt-free blocked claim or the unchanged failed-role
receipt and role left by the terminal blocker. The latter is eligible only
when both fields exactly match the repair record's blocked receipt and role.
The controller authenticates the current remote passport and exact cached
blocked-receipt identity before clearing those stale claim fields. It does not
resolve a stage during recovery. Ordinary reconciliation then makes the one
authoritative state-machine call for the transition attempt; that call
authenticates the signed repair and selects its owner or a higher-priority
dependency transition. Mismatched, partial, unauthenticated, or active-role
claims remain blocked, and invalid repair evidence fails before a provider
call.
If deterministic preflight requires an operator ticket fix after that active
repair is prepared, the state machine may rebind only a repair with no owner
success to the exact authenticated forward passport migration from its signed
head to current HEAD. The superseded signed record is archived, the active
record is re-signed at the new head and tree, and the attempt count is
unchanged. Missing, ambiguous, rewritten, cross-Factory, dependency-repair, or
post-success lineage refuses with `repair_record_head_moved` instead of
re-running the original resume-commit validator.
For a retained contract-block terminal, the controller materializes the block
but does not request resume until the committed ticket visibly names that exact
current blocker receipt. This check grants no authority—the state machine still
authenticates the unique directive commit—but an absent or older receipt is an
ordinary wait rather than a recovery error.
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
An idle qualification claim blocked by Planner preflight may retry without a
passport only when it has no run record or active process, its prior Planner
receipt is unconsumed, its cell is clean and remote-equal, and its route names
the current Factory. The controller reacquires only that ticket lease, issues
a fresh state-machine receipt, and reruns sealed preflight. Failure releases
the lease and keeps the same block; before release the controller persists the
redacted failure lines, exit code, reason code, and full-output digest in one
bounded event. Malformed or oversized refusal output creates a distinct
fail-closed evidence block. Only a pass reopens ordinary Planner
reconciliation. Passport, terminal, submitted, dirty, drifted, unlisted, and
stale-route claims remain closed.
An idle passport-bearing Planner preflight block keeps its exact current
unconsumed receipt and accepts only one pushed direct child changing that
ticket's four readiness-owned fields. The child names the refused receipt and
signed failure event, preserves every other byte including protected-test
authority, and must pass the existing readiness validator. The ordinary state
machine is bound to that checked head, issues the parent-linked successor, and
sealed preflight alone reopens the claim. Restart adopts that exact successor;
unrelated paths, rewritten ancestry, dirty or remote-divergent cells, active
work, and mismatched passport/event evidence remain blocked.
The same provider-free retry accepts a controller `worker-error` only at that
exact preflight boundary. It additionally verifies the prior receipt digest,
lease digest, ticket blob, head/tree, route bytes, current Factory SHA, single
certified push origin, and shared product Git directory. A retained exact lease
may be renewed and an explicitly released exact lease may be reacquired; a
cross-release, cross-ticket, foreign-repository, or otherwise inexact claim is
not reclassified as preflight work.
During a protected qualification, the development scheduler authenticates a
durable Contract 1.7 Planner, Test-author, or Builder contract-blocked result
against the exact qualification kit SHA before returning only that ticket to
`Backlog`. Outside qualification it retains the ordinary
`Blocked-Escalated` workflow. In either case its lease is released and sibling
lifecycles continue. A qualification Spec-linter `FAIL` also returns the
ticket directly to Backlog instead of entering the ordinary replan/round-three
authorization loop.
Every ticket mutation passes one action-aware state whitelist, including
Reviewer reconciliation, operator resume materialization, and qualification
backlog return. Planner/Spec-linter, Builder/Reviewer, and authenticated
contract-repair loops derive their attempt number from existing durable ticket
or signed repair evidence. Each transition receipt binds that number and the
controller appends it as a typed event. Planner/Spec-linter waits before round
three and every later round; contract repair waits before the fourth and every
later repair. One exact ticket-only `OPERATOR AUTHORIZATION: <role> round <N>`
line permits only that next round through the existing plan/apply control and
passport migration boundary. Missing, duplicate, stale-round, wrong-role, or
unrelated changes remain provider-free waits or typed refusals. Builder/Reviewer
remains budget-only. Repair replays keep the coarse business state for
operator legibility but are no longer invisible.
The first task-submitted terminal failed Cursor attempt for a protected
qualification keeps its claim and authenticated evidence while the controller
appends the existing same-family direct-CLI fallback and resumes the same
deterministic stage. The
fallback atomically converts an initial v1 route plan into a same-release v2
journal before appending its revision, preserving the original plan bytes and
provenance. Because that trusted fallback snapshots permitted partial role
changes, including a Builder's current-ticket root-cause log but never sibling
tickets, tests, or Factory controls, it runs before terminal passport export;
the resulting clean exact head then receives the failed charge through
preserving passport migration.
When the certified product origin is GitHub HTTPS, only fallback and route
migration Git network subprocesses receive the owner-authenticated GitHub credential through
the host-scoped `gh auth git-credential` helper. Readiness probes, model
adapters, local Git operations, URLs, arguments, repository configuration, and
durable fallback evidence remain credential-free.
An unsubmitted durable-GO terminal is not a model fallback. The controller
exports its conservative charge into the ordinary passport, blocks it under
the same release, and leaves only the authenticated successor recovery path.
It is idempotent across controller restart. A second task-submitted
attempt for that ticket, role, and frozen candidate is refused as no progress
instead of replayed; preserved attempts from predecessor candidates do not
consume the successor's one fallback boundary.
When that failure predates the executing sealed successor, its failed manifest
and route journal remain bound to the exact older Kit-SHA while the local
successor qualification manifest must bind the executing release SHA. The
handoff makes the cell clean before the ordinary release-migration revision;
an unsealed, non-successor, differently headed, or differently routed recovery
still fails closed.
If that release-migration revision already follows the handoff when the
controller restarts, recovery validates the complete journal, requires the
exact fallback revision and a suffix containing only release migrations, and
finds the unique ancestor commit carrying the fallback revision trailer. The
successor reopens that terminal receipt as running so the ordinary finish path
exports its charge exactly once before resuming the role.
A fallback is one per exact authenticated failed-attempt generation, not one
per ticket lifetime. A later release migration may therefore retain the old
append-only fallback while a different role/run/manifest appends one fresh
revision; replay of either exact failure remains idempotent. If an exact
fallback was applied after the controller parked the claim, reconciliation
first reacquires its ticket lease and reuses the ordinary finish path to
migrate the passport and clear only the failed receipt fields.
An exit 143 before task submission reopens only that interrupted role, and only
after the current signed passport, clean cell, branch, and remote head agree
exactly; every submitted or differently terminated interruption stays blocked.
An exit 125 after durable GO but before a durable submission marker retains the
full conservative reservation and remains blocked under the same Factory
release. A successor release may reopen only that zero-progress terminal after
the signed passport proves the charge was exported exactly once and the clean
remote passport still agrees. New terminals name the typed
`adapter_submission_unconfirmed` reason and retain a bounded output digest for
diagnosis; the exact legacy empty-output, empty-reason shape remains readable
for upgrade recovery. A repeated failure under the successor stays blocked, so
release recovery cannot become a same-release retry loop. Submission markers
use collision-resistant owner-only temporary files before atomic publication
and directory synchronization.
The controller also carries one bounded recovery attempt inside the exact
ticket claim. It binds the Factory release, recovery name, claim state and
receipt, qualification generation, passport, run evidence, Git head, and exact
ticket bytes. Three identical no-progress outcomes leave a lease-free
`recovery-abandoned:<name>` claim and emit one digest-only event. A changed
authenticated input or outcome starts a new count; an unchanged abandoned
claim is never recovered again. A recovery may instead report one uncharged
wait only through its ticket's thread-local context while the exact current
transition receipt remains digest-valid and unconsumed. Receipt recognition
durably restores the prior attempt state before returning; the shared recovery
boundary then rechecks that receipt. Stale, consumed, mismatched, failed, or
sibling evidence settles normally. The attempt retains the exact pre-recovery
blocked reason, so changed authenticated evidence or a successor release
restores the real recovery selector instead of leaving the abandonment label
as a dead end. Claims that are blocked, waiting, or budget limited release any
inactive dispatcher lease both before recovery and after a role drains. Active
roles and sibling claims are untouched, and a correctly reattached parked
branch remains eligible for the ordinary one-use recovery.
The same release boundary applies to a typed `launch_void`: release migration
keeps its receipt runnable until the ordinary terminal reducer clears it only
when the manifest proves abandoned phase, no GO, no submission, zero cost, and
the launch-void cost basis. A same-release or malformed receipt remains blocked.
This classification is required before restart-capacity selection so recovery
cannot be stranded outside the worker.
A role subprocess that exits without any receipt-bound terminal manifest is an
invalid launcher boundary, regardless of exit code. The controller preserves
the receipt, blocks the claim, releases its lease, and emits one content-free
diagnostic instead of clearing the receipt and resolving the same stage again.
Every successful mutating role must retain its authenticated role-input commit
as an ancestor. The trusted wrapper checks that invariant
before reading the remote for publication or attempting a push. If a clean
provider output rewrites that history, the wrapper preserves the exact output
under `refs/factory/failed-role/<ticket>/<run-id>`, restores the local ticket
branch, index, and worktree to the unchanged authenticated input, revalidates
the unchanged remote, and records `role_exit_history_rewritten` with the full
conservative charge. The same Factory keeps that receipt blocked. A successor
may retry only when the remote passport still binds the role-input head, the
failed charge was exported exactly once with no completed-role evidence, and
every terminal field matches the typed post-GO failure. Test-author's separate
trusted operator reorder command remains the sole ancestry-rewrite exception;
role execution never performs that rewrite and the controller never
force-pushes either path.
The same restoration happens when bounded role-output validation rejects a
clean descendant commit before role exit. The rejected commit remains under
the failed-role diagnostic ref, while the ticket branch, index, worktree, and
remote return to the authenticated input so the typed invalid-output retry
cannot inherit unreviewed local history.
Before those publication checks, the wrapper reconciles the selected ticket's
physical mode with its committed `100644` mode. It changes only an owner-owned,
single-link regular file from `0600` to `0644`; symlinks, hardlinks,
executables, foreign ownership, group/world-writable modes, or a non-`100644`
Git entry fail the role before push. Existing content and protected-evidence
checks still run afterward, so mode repair cannot bless dirty ticket bytes.
The same quarantine boundary applies when a clean Planner, Spec-linter,
Builder, or Narrator commit changes protected ticket evidence. The wrapper
preserves the rejected head under the failed-role ref and restores the exact
unchanged remote input. A successor may reopen only that role after the signed
passport contains the failed charge once and no completion. Legacy releases
that left the rejected head checked out additionally require the existing
protected in-flight rewrite authorization before passport migration can move
back to the unchanged remote input. Same-release retry and acceptance of the
protected mutation remain forbidden.
See [factory-runtime.md](factory-runtime.md) for the schemas and commands.

Ticket content is read from the launcher's validated ticket worktree, while
controls and the operator map overlay remain anchored to the registered
product root. Operator-map projection reads the committed exact ticket branch rather
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
latest successful Reviewer and Narrator runs, reviewed SHA, post-review
ticket/bundle controls, bundle Git blob, and the unique exact open PR, then
records Awaiting Approval. Historical merged or closed PRs on the fixed ticket
branch remain lineage evidence but never compete with that current open
publication candidate. Ticket-PR and bundle attestation share one
fail-closed Narrator-evidence classifier: only bounded ordinary PNG blobs under
the current ticket's evidence directory are admitted, additions and updates
must be referenced by the current bundle, and deletions must have been
referenced by the reviewed bundle. The classifier validates the complete PNG
chunk stream and refuses unreferenced, nested, sibling, executable, malformed,
or excessive evidence. The subsequent approval head is validated by one shared
ticket-PR/attestation helper: it must be the direct child of the exact bundle
attestation commit, change only the current ticket and a newly added ordinary
approval receipt, preserve all ticket text except the sealed
Awaiting Approval → Approved receipt transformation, and bind the same reviewed
SHA, repository, branch, Kit-SHA, PR, bundle blobs, merge method, and ordered
timestamps. A later sealed successor may append a validated release-migration
route commit and replace only the ticket's Kit-SHA; the approval and bundle
receipts, bundle document, and all other approved ticket text remain
byte-identical. Phase two may use that immutable approval receipt after the
operator map projects the transient approval overlay away; partial overlays still refuse.
Even when the deterministic stage reports that auto-merge was requested, the
controller reasserts and verifies the exact GitHub request under the active
publication lease before waiting for merge. `approval` consumes only a newer exact operator-map
Awaiting Approval → Approved overlay, commits the approval attestation, and
requests normal protected GitHub auto-merge for that exact PR head. `done`
requires the exact merged commit on authoritative `origin/main`, all configured
post-merge contexts successful on that commit, and projects accounting into a
separate closeout branch with a terminal attestation and Done ticket. It never
bypasses protection, force-pushes, or lets the dispatcher manufacture approval.
Missing or pending post-merge contexts keep closeout waiting; a completed
unsuccessful context remains a fail-closed controller error.
An authenticated merged passport enters closeout before dependency refresh,
even when a prior wait already released its publication lease.
An open closeout PR is a controller wait. After it merges, retrying `done`
revalidates the exact protected-main Done receipt, ledger, original merge and
checks, and closeout merge. The controller records one idempotent
`operator_terminal_recorded` event (`{protected_main, terminal_basis}`) before
it emits completion and releases the ticket — Done has no external witness;
this is Git- and receipt-only. An unconfirmed Done leaves the claim retryable;
stale prepublication dependency logic is never reopened.
For qualification, validated protected-main Done is authoritative even when
the sealed registered checkout remains intentionally detached with a
nonterminal ticket. That target is excluded from admission and cannot be
reclaimed. Outside qualification, Done in the product root suppresses
passport-based claim recovery.
Any residual claim is renewed and released before scheduling, while the
historical passport remains available for audit and reduction.
Qualification restart and recovery count those protected Done targets together
with runnable claims, retain the complete cohort in their boundary events, and
schedule only unfinished tickets. The controller also records one
candidate-bound completion event for each claimless protected Done target.
Every qualification event also binds the exact protected manifest generation
and canonical manifest digest. Reduction selects only that boundary, rejects
partial or malformed current-boundary metadata, and leaves older same-release
event files immutable and auditable. In
successor qualification it first verifies the unique authenticated release
suffix from the manifest's production source through the passport's current
pre-candidate release, then uses the surviving clean ticket cell to migrate the
merged terminal passport without a claim, role, publication lease, or charge.
If signed post-merge route migrations advanced the ticket head, both adoption
and final reduction require one unique contiguous v2 passport suffix from the
Done-approved PR head through the current passport head, Factory, protected
base, route, and parent digests. Historical membership, disconnected or
ambiguous suffixes, and substituted passport parents remain invalid. Route
migration refuses before preview or approval whenever an existing bundle
attestation binds a different Kit-SHA; evidence is never silently rewritten.
The typed terminal-adoption marker and event seal the manifest source, immediate
passport predecessor, source/candidate passport digests, and protected Done/PR
identity.
A valid marker and exact event make later reconciliation a no-op. The reducer
exempts only that adopted ticket from a candidate publication lease pair and
rejects duplicate adoption, completion, acquisition, or release evidence.
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
resolves the merged implementation PR by the exact number sealed in the
protected approval attestation, binds its approved head and the protected
bundle/approval blobs, and
refuses status/check name collisions. It projects and commits once, then owns
creation/reuse and protected auto-merge of the exact closeout PR. A retry
revalidates the same remote commit. Only valid attested Done on protected main
produces sequencer action `COMPLETE`, after which the dispatcher releases the
lease; closeout PR creation is never terminal evidence.

Before bundle attestation, the sequencer checks the Narrator artifact for the
same required sections and approval question as the trusted attestation path.
When that artifact is structurally invalid and no bundle attestation exists,
one additional Narrator run is allowed. Every later correction waits without a
provider for the exact next `OPERATOR AUTHORIZATION: narrator round <N>` line
and reuses the same ticket-only plan/apply and passport-import boundary as
other semantic rounds. One line grants only that round; stale, duplicate,
wrong-role, or unrelated changes remain blocked instead of entering operator
approval.

The Done receipt always binds the hash of the complete projected ledger. Its
closeout commit includes `factory/ledger.csv` only when projection changes the
tracked bytes; a prior concurrent closeout may already have projected the same
terminal run set. Ticket and Done-attestation files remain mandatory in every
closeout commit, and no other paths are allowed. Protected terminal validation
checks that hash at the immutable closeout commit and requires the current
ledger to retain every attested run ID with the exact same row. New or
reordered rows are allowed; missing, changed, duplicate, malformed, or
header-incompatible rows fail closed. Projection seeds durable history from
the exact protected-main closeout worktree, never a possibly stale runtime
checkout, then adds the authoritative settled manifests.

Contract 1.8 exposes `emergency-admit` for one otherwise unchanged pre-provider
role receipt. Its read-only plan binds an open Factory issue, non-automatic
operator, bounded reason and expiry, current release SHA/tree and trust scope,
project/origin, ticket branch/head/tree/blob, route, authenticated passport,
lifecycle and Resume-State, empty publication authority, current lease, and one
unconsumed deterministic role receipt. Apply requires the exact plan hash and
creates an owner-only HMAC-authenticated immutable authorization. The launcher
consults it only after normal receipt consumption rejects; it consumes the same
receipt once before the unchanged runner performs budget, concurrency,
credential, provider, and accounting admission. Provider or terminal evidence,
maintenance, loop caps, drift, replay, ambiguity, or a closed issue refuses the
fallback. The controller archives use against the unique run manifest and
authenticated passport charge. It cannot select another role, mutate state or
evidence, or reach approval, merge, publication, CI, or terminal gates.

Contract 1.8 also exposes a narrow emergency plan/apply form through
`ticket-attest` for an exact already-merged ticket whose normal approval chain
cannot be completed. The read-only plan binds a current open GitHub issue,
explicit non-automatic operator, bounded authorization window, protected-main
commit/tree/ticket blob and state, exact merged PR and configured successful
checks, active kit, and authenticated passport snapshot. Apply requires the
plan also binds either an idle blocked claim or a matching controller-signed
pause, including an exact idle budget pause, with no lease or publication
capability.
Its SHA-256 apply reuses the ordinary ledger projection, closeout branch, push,
PR, and protected auto-merge. It records a distinct terminal receipt rather
than synthesizing bundle or operator approval. Exact operator-built work may use
an explicitly passportless basis only when protected main says it was built
outside the Factory and controller claim/passport records are both absent.
Retries accept only the already-committed receipt and original approval hash;
the terminal reader independently revalidates commit topology, authorized
paths, source ticket blob, receipt digest, timestamps, and ledger containment. A
merged emergency closeout uses the same protected-terminal-first exact Done
projection as ordinary closeout.
One versioned extension may also retire exactly one bundle-only partial chain
whose internally validated bundle names a kit other than the active closeout
kit. The plan binds its exact protected-main path, blob, and prior kit; apply
deletes that path in the same protected closeout commit. The terminal reader
independently revalidates the prior artifact and requires its deletion.
Current-kit evidence, approval/done partials, malformed bundles, changed
artifacts, or additional path changes remain invalid and cannot use this route.
Successor qualification reconciles that terminal against the unchanged
authenticated source passport and its exact signed idle pause; it does not
fabricate an Approved/merged passport. The reducer retains historical roles
and spend, attributes no provider spend to the successor, and excludes only
that ticket from publication replay. Evidence drift or duplication fails
closed. The emergency receipt may name the current candidate or an earlier
candidate in the active qualification environment's content-addressed receipt
chain; arbitrary historical Factory identities remain invalid.

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
Activation and mutating model-route migration use the same strict authorization
parser. Migration revalidates current protected main, the selected remote ticket
head, repository, source and target kit, branch, and state before changing Git.
The first application starts at the exact authorized head. An interrupted
idempotent retry may start only at its one direct child when that commit changes
only the selected ticket's Kit-SHA and its exact append-only release-migration
journal, with both paths committed as regular `100644` blobs; sibling entries
remain neither consumed nor reinterpreted. Every other
head, state, path, or route-history change requires a new protected
authorization.
The historical T-198 semantic-authorization recovery consumes that same
protected entry itself after its first passport edge. It binds the writer's
preview and readiness hashes, requires the legacy plan to become the writer's
two-revision v2 journal, and accepts only the exact pushed two-path child
authored by `Software Factory <factory@local>` before recording the second
passport edge. A crash after the commit or push resumes through the sealed
writer from that exact child, and the passport advances only after the
certified remote matches it. No other
blocked or abandoned claim receives automatic migration authority.
Qualification upgrades bind liveness to the non-overlapping controller lock
and active-run markers. A terminal orphaned `running` claim remains portable
state for the successor controller rather than an upgrade deadlock.
Upgrade recovery keeps the claim blocked while it first authenticates the
current clean head into the successor passport. This pre-route boundary lets an
exact protected rewrite authorization bind the unchanged old route digest.
When the successor reacquires a dispatcher lease, it clears the prior lease's
released marker in the same durable claim update; the recovered lease is then
renewed by ordinary scheduling rather than mistaken for an already-released
lease and claimed a second time.
An upgrade never clears a receipt that still has terminal run evidence.
Successful terminals remain available to terminal-export recovery; recognized
failed terminals remain available to their exact typed recovery; unknown
failures remain blocked. Only a receipt with no terminal manifest may be
cleared as stale cache after the passport and route migration authenticate.
Before typed recovery clears a failed claim, the terminal charge must already
appear exactly once in the authenticated passport. Legacy push failures and
pre-submission interruptions invoke the receipt-bound passport export when
that charge is absent. If an authorized head rewrite blocks that export, the
controller first migrates the head and then retries the same bound export; a
clean remote head alone is not accounting evidence.
One predecessor release produced a narrower false terminal after Cursor had
reported one final success and Builder's push had already converged: the
manifest was `abandoned`/`abandoned_conservative`, GO and submission were both
durable, exit was 128, and role exit and terminal reason were blank. A
successor may correct only that exact Builder shape without replay. The trusted
passport helper requires the consumed `RUN builder` receipt, unique run,
owner-only output and progress journal, exactly one final progress success,
exact exported charge, clean current cell, current recovery Factory, and either
the direct receipt passport or one unique contiguous authenticated migration
suffix. Only the production `cursor-openai` and `cursor-anthropic` adapter IDs
are eligible; legacy and non-Cursor adapters fail closed. When the failed run's
authenticated export advanced from its receipt input to the successful Builder
output before release migration, correction accepts only one all-v2 successor
suffix whose source is a strict descendant of that input and whose last source
passport binds the current passport parent. Generic receipt lineage remains
unchanged. The controller additionally requires passport, cell, and remote head
convergence before and after the HMAC-signed correction, then clears the claim
through its ordinary durable update. Missing artifacts, stale Factory state,
tamper, ambiguity, any other terminal shape, or repeat evidence fails closed.
The correction remains authenticated audit evidence on later exports; it is
not a generic lifecycle override. Emergency override authority remains
separate and requires explicit owner authorization bound to its target plus a
linked GitHub issue before use.
An older Cursor catalog may likewise reject a completed Spec-linter run solely
because its exact reported model identity changed. Successor recovery is
limited to the authenticated old-plan mismatch whose owner-only output names
the current catalog identity, contains one terminal success and the exact
identity-rejection diagnostic, and whose progress journal independently ends
in one success. The local history must be exactly receipt input, one
ticket-only output commit, its exact revert, a bounded contiguous chain of one
or more authenticated ticket-and-route migrations ending at the current kit,
and optionally the controller's ticket-only revert-of-revert. The active route
selection is resolved across that journal because an unchanged release
migration intentionally carries only its prior-resolution digest. Recovery
first requires the passport and remote to converge on the migration tail. The
controller then restores the exact conflict-free three-way ticket delta on top
without force, preserving the migrated Kit-SHA and route bytes, exports the
failed charge once, and records the HMAC-signed completion correction. A merge
conflict, changed replay, extra path or commit, different model, result, route,
remote movement, or same-release failure remains blocked and never enters
provider fallback. This recovery accepts the original blocked claim and the
idle claimed or running forms produced by release-upgrade normalization; an
active role is never recovered, and all receipt, terminal, passport, and remote
proofs remain identical.
The state machine never migrates a passport for a `REFUSE` transition; the
controller blocks the claim first so the next one-shot owns that boundary and
its durable pending marker.
After the preview-bound route migration commits the successor Kit-SHA to both
the ticket and route journal, a second ordinary descendant migration updates
the passport route digest and only then reopens the claim. A durable
controller marker makes the between-migrations restart boundary idempotent.
The exact T-198 occurrence abandoned by the predecessor implementation is
readmitted only when its authenticated terminal, migrated passport, pending
marker, released lease, pushed authorization head, retry reason, attempt count,
and active release all still match. Generic `recovery-abandoned` state remains
terminal.
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
An accepted successful Test-author push with a merge-rich tail uses a distinct
authorization schema at the same new-head-keyed path. The successor first
migrates the authenticated passport on the unchanged old head. A canonical
one-file protected-main commit directly above the replay base then binds the
old signed passport/head/tree, new head/tree, branch, repository, route,
current Factory, and the accepted Test-author's historical Factory, run, and
receipt. Verification requires the exact completed evidence and charge whose
recorded input head is the parent of a late test commit; it also proves the old
line violates ordering, the new line satisfies it without mixed commits, the
final trees and non-exempt patch multiset match, and every protected merge tree
and second parent are unchanged. The controller does not migrate until its clean
cell head is the exact remote ticket tip. Only an operator's explicit
`--force-with-lease=<old-head>` can publish the authorized rewrite, and a
restart after recovery is a no-op.
An exact failed Test-author push that repaired mixed commit topology uses the
separate `ticket-history-repair-authorization/v1` schema. The protected record
binds one open issue, explicit operator, maximum 24-hour window, failed run and
receipt, the current recovery Factory and historical failed-run Factory, old
remote head, new head/tree, passport, route, and exact
force-with-lease value. Its `authorization_parent` is the protected-main commit
directly below that one-file record; its distinct `replay_base` must already
belong to the signed passport's base history and be the common base of both
ticket histories. This allows unrelated protected-main advancement without
pretending it was the ticket replay base. Verification requires the old line
to contain mixed or late test history, the new line to satisfy both ordering
rules, identical per-path non-Factory patches, unchanged protected merge trees
and second parents, and no final-tree delta except one append-only current
ticket log. Migration preserves prior success evidence, adds the failed
conservative charge exactly once, and never promotes that failed run to
success. The historical Factory must occur in the authenticated passport's
release history; successor migration never relabels its receipt or manifest as
new evidence. The controller therefore resumes ordinary Test-author work after the
operator publishes the exact authorized head; no role, CI, approval, or
publication gate is bypassed.
An exact `Blocked-Escalated` ticket remains blocked during normalization; only
its existing authenticated resume receipt may restore the recorded resume state.
After activation, the operator uses the existing preview-hash-bound `models
migrate` flow. A v1 plan becomes a v2 journal; an existing v2 journal receives
one parent-hashed release-migration revision that preserves every prior
revision and the active resolution. Both paths update the ticket Kit-SHA before
work is reclaimed. The initial v1-to-v2 schema-migration revision may preserve
the same Kit-SHA; only a later `release-migration` revision claims a release
change. A release migration binds an unchanged active resolution by its
canonical SHA-256 and includes a full refreshed resolution only when physical
route evidence changes; legacy full-resolution revisions remain valid. In both
cases the embedded legacy bytes, digest, policy, selections,
pin commit, and revision hash chain must match exactly. This authorization
never changes ticket state, migrates a branch, renews a lease, or permits an
unprotected-main record.
The ordinary migration preview carries compact source, readiness, journal-tail,
and approval digests; an explicit diagnostic flag includes the complete
candidate journal. Apply probes readiness once and requires its exact digest
from the approved preview, so changed readiness refuses without a second probe
round or a weaker journal check.

A one-time Contract 1.2 migration may instead use the separate
`factory/migrations/contract-1.3/` legacy-closeout format. It does not create or
satisfy ordinary bundle, approval, Done, or route-plan attestations. The local
`scripts/legacy-closeout.py` generator reads immutable Git evidence and GitHub
PR/check metadata, requires settled Reviewer/Narrator accounting, and performs
no commit, push, or merge. Its exact authorization and
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
For normal attestations that cross sealed successors, the reader requires the
bundle's exact route journal as a prefix and permits only a hash-linked suffix
of release migrations ending at the Done and ticket Kit-SHA; fallback or
discontinuous post-bundle routing fails closed.

Overlay-driven state materialization is limited to Backlog-to-Ready and the exact
declared non-sensitive resume from Blocked-Escalated;
factory-owned phases use the transition action. Projection falls back to
committed `HEAD`, never live checkout bytes, when no exact ticket ref exists.
Each newly committed Blocked-Escalated source clears any prior resume overlay
and timestamp baseline before interpreting the remote state, including a
repeated blocker at the same coarse state with no overlay. The first exact
remote Blocked-Escalated observation for that source records the replacement
baseline; only a strictly later remote move may resume it.
Every accepted state overlay is also bound to the exact committed ticket text
from which it was ingested. A later ticket commit invalidates that overlay
before effective-state projection, so a repeated block is published even when
the operator map still shows the prior resumed coarse state. Legacy unbound overlays are
cleared once and must be re-observed.

Before the first role, `models pin` resolves one exact six-role plan and records
it with `Kit-SHA:` in one committed and pushed ticket-branch transaction. Every
later preflight, sequencer call, and run refuses a different physical kit SHA;
roles read only their active journal resolution. A run re-probes only that exact
route and never silently retries a task-bearing process. Contract 1.4 may
migrate the v1 plan and append a fallback revision only after an eligible
terminal GO attempt, one-use operator approval, validated partial-work snapshot,
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
release. Canonical JSON policy and activation inputs keep their bounded size;
the growing SQLite provider database instead retains its owner/type/link and
schema-identity checks without inheriting that JSON payload ceiling. Contract
1.6 and Contract 1.8 capacity one retain the fail-closed
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
Maintenance blocks claims and new role submission while exact authenticated
owners may renew and release solely to drain; activation and rollback refuse
until every lease drains. The kill switch clears only validated safe lease
state after stopping recorded runs.
Renewal serializes only on the dispatcher lease lock, not the provider launch
lock; matching ownership and pre/post mutation KILL checks remain fail-closed
while unrelated provider entry cannot starve a heartbeat. Each heartbeat owns
an isolated process group, uses lock-free signal handling, and publishes its
PID/start identity beside the wrapper so ordinary completion and the kill
switch can apply bounded TERM-to-KILL shutdown without PID-reuse risk.

The retired Contract 1.6/1.7 external supervisor was deliberately one-shot: one invocation
asks the stable launcher for one deterministic claim, starts at most one
ephemeral dispatcher child on `START`, and exits on `WAIT` or `ESCALATE`.
Autonomous claiming requires configured capacity above one so the lease can be
transferred in memory to that child. At the first sequencer-authorized Reviewer
boundary, the trusted ticket-PR helper creates or reuses exactly one open PR for
the clean pushed ticket head. It compares the complete app-bound required-check
rules for protected `main` with GitHub's reported required checks, waits without
launching a role while any required check is omitted or pending, supplies completed failures to
Reviewer, and revalidates successful exact-head checks and Reviewer lineage
before Narrator. If the exact PR becomes definitively red after its bundle is
attested but before operator approval, the same authenticated publication-repair
transaction used after approval withdraws the stale bundle and routes only the
named repair owner back to Building. A later
Builder or Test-author run forces fresh review. The helper has no approval or
merge authority.
Under Contract 1.8, compatibility `dispatch-plan` performs deterministic
admission only and cannot spawn a dispatcher. The release-owned controller is
the sole caller that advances work through state-machine receipts. Concurrent
admission wakeups serialize on a process-scoped lock in the generation-wide
worktree coordinator, not the product launch lock. Candidate and dependency
resolution therefore occurs before the product launch boundary. A selected
claim then revalidates the clean registered checkout, unchanged operator map,
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
Production certification, read-only activation planning, and activation itself
also reject any `factory/QUALIFICATION.json` before receipt issuance,
consumption, or journal creation. A matching candidate manifest is still
qualification-only authority and is never silently ignored in production.
Installation and certification serialize the kit-suite evidence decision under
the install lock. Certification may reuse an unexpired passing suite result for
the exact unchanged sealed release. When the current active generation was
fully measured by the same certification tool and exact Factory SHA/tree,
certification may also reuse that product-suite result for a nonempty descendant
whose complete diff only adds or modifies regular canonical
`factory/tickets/T-NNN.md` files. The committed source activation journal, its
receipt hash, measured result digest, product identity, Contract/runtime tuple,
pin/config hashes, provider-concurrency evidence, and host identity must all
match. The target still runs the sandboxed product repository and secret checks;
the new receipt binds the source generation/receipt/evidence, target SHA/tree,
closed changed-path list, and raw diff digest. Deleted, renamed, noncanonical,
executable, configuration, dependency, classifier, ambiguous, or otherwise
ineligible changes fall back to full product certification. All other product,
config, receipt, and activation validation remains unchanged. Fresh certification
refreshes evidence only after the isolated suite, tracked-tree check, and sealed
release verification pass. Install first proves the SHA is on `origin/main`
and binds exact successful protected main CI. The installed launcher labels
helper runs `production-certified`; the qualification launcher labels its
separately SHA/tree-sealed candidate `qualification-candidate`; and the
contained repository-test launcher labels its forced mock runs
`repository-test`. Repository-test Doctor readiness proves only that sealed
mock boundary, never provider or production readiness, and GitHub-mutating
launcher commands are unavailable. The repository-test controller stops at its
first authenticated Planning claim, runs exactly one sealed mock planner with
durable role-exit enforcement, and then stops without production model
resolution or later role launch. Mutable local kits cannot claim any sealed
scope and remain development-only. Product receipts bind the exact evidence ID/digest
and cannot expire after that evidence. Products may opt into the sealed
`certification-runner.py` with a repository-owned declarative DAG. It records
wall time, CPU, peak memory, cache status, exact input digests, and artifact
digests for every phase; cache hits report saved phase wall time separately
from lookup, rehash, and restoration overhead. It runs at most three workers
and gives each phase a
separate log and temporary directory; and cancels sibling process groups after
the first failure. A passing measured result is bound to one exact Factory
SHA/tree, product SHA/tree, Contract, Node, and npm tuple and embedded in the
certification receipt. The shared `certification-preflight.py` validates that
tuple before readiness tests, qualification materialization, or certification
suites can spawn expensive work. Qualification activation and its sealed
launcher retain and revalidate the same tuple, so the controller cannot inherit
a different shell runtime. Unknown, missing, malformed, or mismatched tuple
data fails closed with typed non-secret diagnostics. Existing opaque
certification scripts without a v2 plan remain compatible. The owner bootstrap
may first run `factory-kit runtime-pin` to verify the product plan's exact
Node/npm/npx executables and atomically place them in `~/.factory/bin`. That
owner-local directory is already first in the sealed launcher's fixed PATH, so
a newer system Node cannot supersede the pin; every later boundary retains its
independent strict tuple check. Cache hits are recorded, but the
runner reuses a phase only when its protected plan explicitly opts into
`artifacts` and declares a nonempty, complete output set. Phases with undeclared
side effects cannot opt in; application tests, policy, security, and
configuration checks must retain the default `never` policy. Products may mark
exact phases with `kind: "test"` and `optional: true` only when every dependent
phase is also optional. Those tests still run by default. An explicit
`--skip-optional-tests` certification or release request omits only those
phases, and the result and receipt record their names. Build, dependency,
policy, security, and configuration phases cannot use this path. Same-workspace
restart reuse retains its self-hashed evidence. Across Factory certification
commands, the Factory imports only HMAC-authenticated, unexpired entries into a
read-only disposable cache input; the product sandbox never sees the persistent
store or key. A hit binds the exact physical Factory tree, product SHA/tree,
raw plan, dependency artifacts, command, Node/npm, runner runtime, and declared
and granted network capability. The runner rehashes the complete declared file
manifest before restoring regular files and their exact modes. Successful raw
logs are not persisted. New entries leave through a separate disposable output
and are independently validated, size/count/TTL bounded, authenticated, and
atomically published under a cache lock. Missing, interrupted, stale,
ambiguous, malformed, symlinked, or tampered evidence is a miss; it never
restores undeclared side effects. Exact protected Factory CI proof and full
product certification remain authoritative outside the closed ticket-control
replay case.
The v2 DAG also binds exact Node and npm identities plus each phase's declared
and granted network capability. Required network without command-scoped review
fails before spawn; a reviewed opt-in does not broaden denied phases. Redacted
phase output, npm debug logs, result evidence, and their digests survive a
failed product-certification workspace. Before source preparation, an existing
active product must match its exact committed activation generation, canonical
path, and origin; a fresh project remains certifiable without an active record.
Certification failure receipts also bind the driver and final exit statuses,
the setup, product, phase, or post-driver cache boundary, and separate evidence
and output digests. A pre-product driver failure retains a
bounded redacted diagnostic and a nonempty fixed reason even when no product
result exists; raw setup output is never persisted.
Passing and failing product-certification receipts also retain bounded host-load
observations from immediately before the product driver and after certification
reaches its terminal driver/cache status: the 1-, 5-, and 15-minute load
averages, logical CPU count, and UTC observation time. This
is diagnostic context only. It does not enter phase or cache identity and never
warns, refuses, retries, or reclassifies a certification result. A failed
receipt binds the observation digest into its identifier, so repeated otherwise
identical null-result failures retain distinct load evidence.
On macOS the protected product wrapper only coordinates its disposable tree;
each declared phase enters exactly one Factory-generated Seatbelt profile.
Production requires both the external-network-denied and reviewed-network
profiles before any phase starts. This avoids unsupported nested Seatbelt while
keeping filesystem restrictions common to every phase and granting external
network only to the exact reviewed phase.
An activated contract 1.2, 1.3, or 1.4 keeps that receipt as the runtime destination
binding for trusted ticket and role pushes. Its `product_origin` is the sole
certified `origin` push URL, which may differ from the fetch URL.

There is no per-product scheduled sync LaunchAgent anymore. `factory/operator-map.json`
is computed on demand from committed ticket state and consumed operator
receipts, not synced by a background service, so activation and rollback have
no scheduled-service migration boundary to carry.

Activation uses `factory/MAINTENANCE` and the same launch lock as role startup.
Launch checks occur before locking, after locking, and before the task GO
signal. Maintenance must be published first; activation then waits for the
launch lock, active-run drain, and dispatcher-lease drain. This ordering prevents a new task from
crossing the release switch.
Ticket-lease validation prefers an exact protected-main Done receipt or
lease-free Canceled state over any retained ticket ref. Those refs are not
deleted or rewritten during planning or activation, so a shared qualification
cell cannot be detached. A genuinely nonterminal protected ticket still uses
the exact remote branch and in-flight release authorization and remains
fail-closed on local, tracking, or remote ambiguity.
Production reconciliation also treats the exact committed Canceled state as
retirement authority for a retained controller claim. It refreshes and
byte-reads the current protected `origin/main` commit rather than trusting a
mutable remote-tracking ref. After any active role drains, the same reconciliation
withdraws publication, releases only that claim's exact lease, emits one
retirement event, and removes the claim before recovery; it never reacquires a
lease or replays role evidence. A crash after publication release is recovered
only when a capability-bound retry plus withdrawal proves that ticket has no
publication state. A retained claim with an exact empty lease has nothing to
release and still retires; malformed lease evidence or a failed publication or
lease cleanup remains ticket-local and cannot stop sibling reconciliation.
Malformed parked lease evidence is loaded only into the existing invalid-ticket
quarantine and cannot enter recovery, admission, or scheduling.
New admission checks the clean registered product ticket's base State before
applying any operator-map overlay. Done and Canceled tickets therefore stay
inert after claim retirement even when a stale resume overlay or retained
ticket branch still exists. A refusal after candidate selection reports that
exact ticket; failures before selection remain lane-scoped. Parked worktrees
and retained branches are neither trusted nor rewritten by this rule.
Qualification reconciliation performs one sealed dispatch shadow immediately
after loading durable claims and before cancellation, terminal, passport,
preflight, or upgrade recovery. Because the operator map is a pure projection
with no external system, staleness has no meaning here anymore: an invalid
result records one bounded admission incident and stops the sweep with no
recovery, claim, model, or provider mutation. The operator uses the existing
sealed qualification `--restore` to reinitialize every selected ticket. A
fresh `SHADOW` or `WAIT` enters the unchanged flow, and the later real claim
still re-reads the map under its normal 600-second contract. Production does
not run this qualification headroom check.
Production candidate selection runs the existing provider-free ticket
readiness contract for every otherwise eligible ticket before either shadow or
claim may succeed. A refusal, malformed success, helper error, or timeout is a
named `invalid_ticket_contract` refusal for only that ticket; eligible siblings
continue, while an otherwise empty cohort waits without creating a ticket
branch, cell, lease, route, or state transition. Production then shadows the
same candidate selection and resolves the active model profile before a real
claim can create its lease or worktree. One healthy
probe portfolio covers the bounded claim batch; the existing post-claim batch
pin remains the race recheck. Temporary readiness waits without claiming, and
invalid or unknown readiness persists one ticket-bound admission incident with
only the strict per-route table. Permanent drift after a successful precheck
retains the exact authenticated claim and records the same incident before any
provider attempt; the controller does not invent partial branch/worktree
cleanup authority. Existing pinned claims remain schedulable before this
admission probe. Installed production Doctor checks the same active profile and
reports all unusable readiness, including typed temporary unavailability, as an
error. The controller returns a bounded ticket result (`waiting` for temporary,
`error` for permanent) while preserving its no-claim behavior, and submits
existing pinned claims before starting new production admission.
Dependency syntax remains validated for every considered ticket. Protected
dependency-terminal truth is resolved only after the authenticated effective
state is Ready or resumable and any ticket Kit-SHA matches the selected kit;
every eligible candidate still receives the complete dependency check.
Qualification continues to require Done for every selected
target and does not count Canceled as completion.

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
Planner, Spec-linter, and Test-author independently evaluate exact generated
fixture values from their frozen initializer or reset; an unproducible value
or a repair scope excluding its required setup correction is a contract block.
For every required serialized test command they also trace fixture setup,
reset, and teardown across criteria. A parent-row cleanup requires child-first
cleanup for every non-cascading sibling dependency inside the exact
protected-test scope; `ON DELETE CASCADE` closes that dependency without a
redundant edit. Missing closure blocks before Builder, preserves valid
Test-author commits, and never grants unrelated protected-test ownership.
Planner and Spec-linter also inspect protected tests associated with each
Builder-owned production file for static import/export allowlists, exact-source
assertions, and snapshots. A conflict is executable only when the operator
declares exact `<test path> => <literal>` evidence in
`Protected-Test-Conflicts` and includes that tracked test in `Fixture-Seams`;
unknown checks and unowned declarations fail before Builder.
An escalated blocker may carry one direct, ticket-only operator-context commit
before its byte-exact resume commit. The context commit appends or exactly
replaces one bounded single-line `OPERATOR ANSWER` paired with the current
blocked receipt. It may also append one or more ordered, unique validated
protected-test conflicts. At most one matching tracked path may still be
appended to `Fixture-Seams` for the final conflict when that path is inside the
authenticated protected `PROJECT.env` `TEST_PATHS`; complete ticket readiness
still applies to every conflict. The answer is non-contract
repair context: it cannot change State, kit, route, contract, provider,
application, test, or CI authority, and it grants no ownership beyond the
existing receipt-bound repair role. Multiple answers, stale receipts, broader
paths, merges, longer ancestry, and `factory/rulings.md` changes fail closed.
Passport migration records the context head but does not bless its diff; resume
revalidates the exact predecessor-to-context commit in either ordering.
Under Contract 1.8, Reviewer-owned Test-author work first routes through one
ticket-only Planner repair that appends a higher frozen-contract epoch. The
sequencer authenticates that exact commit before Test-author, preserving
test-before-implementation history. Planner's runtime PATH blocks package
manager entry points so a contract-only repair cannot launch broad product
suites; the protected CI and certification boundaries retain those suites.
`cursor-opus-v1` is the no-record default. `cursor-balanced-v2`, `balanced-v2`,
and `legacy-balanced-v1` remain available for compatibility with prior
activation records and migrations.
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
