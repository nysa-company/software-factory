# Factory development lane

Use this macOS-only lane to exercise a committed Software Factory branch against a disposable synthetic product or an explicitly isolated product-source worktree before the normal release process. It does not install, register, certify, or activate a kit. Real lanes use an owner-only root directly under `/private/tmp` so Cursor's bounded attempt-local paths remain below its macOS path limits; the trusted test harness keeps its caller-provided temporary parent.

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
bash scripts/factory-dev-lane.sh subscription-plan --adapter codex
bash scripts/factory-dev-lane.sh subscription-run \
  --root <root-from-plan> --approve-hash <hash-from-plan>
```

Choose `codex` or `claude`. The canary uses one existing authenticated
subscription CLI session—never an API key—for four same-account calls. It
copies only the selected session into the lane-local home, reserves $0.25 per
synthetic ticket ($1 total), requires all four calls to overlap, validates exact
output, charges the full conservative reservation, and rejects worktree
mutation. Native Claude additionally receives a separate owner-only home,
configuration directory, temporary directory, and credential copy per attempt;
those roots are removed only after their process groups drain.

The subscription canary copies only its selected CLI session; product lanes
copy their three configured CLI sessions into the owner-only lane root once.
When current Claude stores subscription state only in the macOS Keychain,
`FACTORY_DEV_LANE_CLAUDE_OAUTH_TOKEN_FILE` may name an owner-only `0600`
token created by `claude setup-token`. The controller validates it without
printing it and materializes the existing lane-local Claude credential format;
its issuance-time file timestamp bounds the one-year expiry. The source path,
credential copy, and approval remain lane-bound—no API key, shared Keychain,
or new credential service is introduced.
The native Seatbelt profile grants Claude read-only access to macOS
`/dev/dtracehelper`, which its current executable requires even for version
and authentication probes; write access and host configuration remain denied.
Claude's redundant inner Seatbelt is disabled because macOS refuses nested
sandbox application. The Factory-owned outer Seatbelt remains authoritative,
and each attempt retains its private home, configuration, temporary directory,
credential copy, process group, and cleanup record.
Readiness, version evidence, approval hashing, and role execution then use the
same clean environment and only those copied files from the lane root as their
working directory; ambient authentication variables, the caller's working
directory, and the external Cursor session home are not consulted. If a
required copied session is unavailable, the lane stops before consuming
approval, claiming a lease, reserving budget, or submitting a task.
Each readiness probe gets three attempts with a one-second delay between
misses so a short CLI session-state transition cannot exhaust every retry.
An interactive subscription authorization gets five minutes outside the
noninteractive Factory boundary. If the operator does not complete it in that
window, or a copied OAuth access token has less than five minutes remaining,
the development controller proceeds with the next authenticated
profile-authorized route. An explicit authentication failure falls back
immediately. This five-minute rule applies equally to Codex, Claude, and
Cursor, and one ticket waiting for authorization never blocks its siblings.
Family separation and adapter concurrency ceilings still apply;
the Factory never waits on a login prompt or starts an unauthenticated task.
Retained-product resumes run that probe before hashing the plan and again
before validating it for execution; the internal run reuses the second result
instead of immediately probing the same session a third time.
Every failed role attempt is terminal and charged conservatively. The scheduler
does not retry automatically, including for a pre-submission authentication or
model-availability miss. After the lane drains it reports the failed ticket set
and an exact `product-resume-plan` command as the next control step, together
with the failed stage, completed roles, remaining ticket budget, and retained
root. That explicit same-lane resume revalidates readiness, evidence, clean
heads, routes, effective envelopes, budget day, and the mechanical next stage
before issuing a new one-use approval. A targeted resume resolves a stage and
claims a lease only for its selected tickets. Excluded original siblings may remain
unfinished or blocked; their clean heads, local origins, trees, ticket files,
route plans, and runtime evidence remain hash-bound, so they cannot drift and a
later resume can select a different subset from the original ticket set. An
effective envelope is the owner-only regular ticket override when present or
the lane's global envelope otherwise; unsafe overrides refuse planning. Every
batch also writes an owner-only timing report with
coordinator admission/GO/submission/terminal timestamps, elapsed time,
successful-role replay count, and maximum provider overlap.

If a failed mutating provider exits after creating clean local commits, explicit
resume does not promote them. It verifies the latest failed attempt and trusted
base, retains the exact commits under a lane-local diagnostic ref and owner-only
receipt, restores the unchanged isolated-origin tip, and reruns that failed
role. Accounting remains terminal and unchanged; ambiguous history refuses.
If the resume basis drifts after its one-use approval is created but before
provider GO, the controller archives the exact unused approval and resume
basis, invalidates the approval, restores the original ticket selection, and
submits no provider call. Checkpoint export or a new resume plan may then
proceed through the existing controls.

Before creating a resume approval, the controller refreshes the isolated native
Codex and Claude credential copies from their owner-controlled source files;
the new digests are therefore approval-bound. A blocking transition also records
its actual current phase as `Resume-State`, and resolution materializes that
phase rather than assuming Planner.

Each completed ticket publishes an owner-only readiness record after its lease
and role process drain. A selected-ticket `product-export` checks only that
ticket's provider attempts, lease, claim, head, evidence, and worktree, so an
approved sibling can be exported while another ticket continues.

The product scheduler launches every eligible ticket without interpreting
provider capacity. The coordinator owns atomic admission and may wait up to
fifteen minutes only for transient concurrency capacity in this marker-bound
development lane. The runner keeps the ticket lease heartbeat active and
releases the product launch lock while waiting, then reacquires it and
revalidates controls before proceeding. Budget, policy, identity, and rate
denials remain immediate. Development activation permits four calls for one
Codex account or one native Claude account. Cursor remains capped at two
until a real-provider canary validates a higher limit. Each concurrent Cursor
attempt receives an owner-only home, configuration directory, data directory,
temporary directory, and credential copy, so an unrelated legacy scratch
bridge neither disables Cursor nor blocks product planning. Legacy serialized
Cursor lanes retain their existing fail-closed bridge claim.

Product lanes activate `cursor-balanced-v2`: authenticated Cursor routes are
first for every role, with direct Codex and Claude routes used only when the
matching Cursor route is unavailable. Planning rejects any resolved route set
that violates that priority.

Concurrent CLI adapters request GNU timeout foreground mode so timeout and the
provider remain in the Factory-owned process group. The sandbox process
inspector reports the same kernel-derived start identity as host `ps`;
targeted cancellation refuses identity drift and drains only that group.

The PR-less development Narrator marks Preview and Screenshots as backend-only
N/A when the frozen contract has no browser or visual surface, including an
HTTP API. For a visual contract it marks both sections and every affected
criterion `Deferred — publication visual gate`, then names the exact preview,
viewport, reference, and comparison work required before merge. The bundle
remains explicitly development-only and not a production attestation. A
deferral lets the isolated lifecycle and export complete; it is not a pass and
the trusted publication step must resolve it before merge. Normal production
Narrators still require the real preview and screenshots.

Fresh product lanes derive their lane-local machine-day cap from the validated
isolated product envelope. A seeded lane retains its stricter cumulative
accounting override. Neither path reads or changes the host production cap.

## Isolated product proof and resume

`product-plan` accepts one to four tickets after the dedicated four-call mock and subscription proofs establish the capacity ceiling. A seeded retry may select one to four unfinished tickets, so completed siblings are not rerun. The source must be a clean isolated worktree; the canonical Nysa checkout is refused. The lane clones it into a private product, replaces its remote with a local bare origin, and has no GitHub or Linear route.
When that product has a committed `package-lock.json`, the trusted controller
runs pinned `npm ci` separately in every ticket worktree inside the lane
sandbox before any provider role. The provider receives the host's Node 22
toolchain read-only and ticket-local writable dependencies; installation
failure or tracked-tree drift fails planning closed.
Before each product role, the development scheduler uses the shared trusted
ticket-state helper to enforce the Hermes phase sequence: Planner and
Spec-linter see Planning, Test-author and Builder see Building, and Reviewer
and Narrator see Review. Any mismatch stops that ticket before provider GO.

Seeded retries require an owner-only, single-link accounting manifest bound to the exact seed-bundle digest and approved base SHA. They also require an owner-only lineage record in one shared artifact directory; it binds the manifest digest, a lineage ID derived from the base and complete historical ticket set, and the previous manifest digest. Create it only with `product-seed-lineage --accounting <manifest> --output <new-record>`; add `--parent-accounting <previous-manifest>` for a successor. The helper requires all artifacts to share one owner-only directory and will not overwrite a record. Caller-selected IDs are rejected. An atomic lineage-head advance permits exactly one child of a cumulative accounting snapshot, so separately authorized sibling lanes cannot both spend from stale totals. A busy, reused, detached, or stale lineage stops before lane creation. Its full historical ticket map is retained even when only a subset resumes. A seed resets role sequencing to `Ready` and discards prior role verdict lines because runtime role evidence is lane-bound and cannot be inferred safely from Git history alone. V2 keeps the default $100 ticket and $500 aggregate ceilings; an explicit operator-authored v3 record permits only the bounded $200 ticket and $700 aggregate development-proof ceilings. V4 permits an operator-authored per-ticket cap map up to $350 per ticket with an exact $1,000 or $1,500 aggregate ceiling. V3 and V4 also bind one authorization nonce and UTC budget day; planning consumes that nonce through the same lineage transaction, and day drift stops before reservation. Prior reservations reduce both limits, and the resulting per-ticket envelope remains the coordinator's atomic admission cap. Missing history, reuse, an exhausted selected ticket, aggregate exhaustion, unauthorized limits, bundle/base/lineage drift, duplicate tickets, seeded symlinks or submodules, and unsafe ticket files fail before provider execution.

When a corrected development kit must replace the kit pinned by a drained
failed lane, `product-checkpoint-export` may retain only the exact successful
Contract 1.7 prefix before Reviewer. The owner-only checkpoint binds the old
kit, base, trusted local branch heads, route evidence, successful manifests and
outputs, and the aggregate seed bundle. Wrapper-failed attempts are charged but
never carried as successful. A v5 accounting successor binds the checkpoint,
its full historical-ticket charges, its exact parent accounting digest, and a
fresh authorization nonce; the lineage transaction consumes both the nonce and
checkpoint digest once. Because checkpoint export cannot reserve or spend, a
fully drained lane may export after its original UTC budget day ends; the v5
successor still requires a fresh current-day authorization. A later product
plan may import checkpoint records for
only a subset of its tickets, leaving omitted tickets at the clean source
`Ready` boundary while retaining their historical spend. Import discards old
routes, pins and approves the new kit and routes, and reproduces the exact next
stage before issuing an approval. An authenticated checkpoint from the older
development scheduler may advance from Ready only through the legal shared
Planning and Building transitions before its next role. Imported Spec-linter verdicts remain an exact
prefix of the ticket log; later current-lane verdicts are accepted only with
matching successful current-lane ledger evidence. Reviewer and Narrator are
never checkpointed and must run under the new kit before `product-export`
succeeds.

Checkpoint evidence and its seed bundle both resolve ticket heads from the
lane-local bare origin. Worktree remote-tracking refs are caches and never
checkpoint authority.

Import also retains an owner-only exact copy of the source checkpoint. A later
checkpoint export verifies that copy against the imported digest and
product-source binding, then prepends its exact role records to the current
lane's successful records. This permits another corrected kit to continue the
same pre-Reviewer sequence without losing prior output hashes or replaying
successful roles; copy drift, import drift, or a detached imported head refuses
export.

Role instructions identify each disposable database environment through a
worktree-relative path, never the lane's physical temporary path. A shared
sentinel rejects any newly added absolute `nysa-sf-dev.*` path before the
trusted host pushes role output and again at checkpoint import and export, so
a retained contract cannot bind itself to either its old or current lane.
Checkpoint boundaries use the invoking trusted controller's sentinel after
the retained lane's exact kit pin is validated, so an older kit need not
contain a checker introduced by the correcting controller.
Seed import first proves the linear approved-base ancestry and authenticates
the first commit's exact Factory lane-control identity and path scope. Its scan
then begins at that commit, excluding the old Factory-owned `PROJECT.env`
rewrite while still checking every later route and role commit. Replay skips
the old control commit, so the new lane's freshly bound `PROJECT.env` remains
authoritative.
Targeted checkpoint records may cover a subset of the original lane, while
their charge map keeps every original ticket so cumulative accounting and
lineage cannot forget excluded spend. Repeated chaining takes that full key
universe from the retained checkpoint rather than the new lane's selected
ticket list.

For sequential protected-base refreshes, keep using the existing committed
`ticket-refresh/v1` attestation path. It already invalidates the old approval
and requires a fresh Reviewer and Narrator bound after the exact merge. This
lane does not add a second refresh or checkpoint format.

`product-export` binds each application artifact to the latest successful
Reviewer head, rejects later non-Factory changes, and excludes the complete
reserved `factory/` namespace. The exact Git bundle retains detailed role,
retry, and audit history only; it is never an application artifact. The
canonical `T-NNN.mbox` projects the approved application tree into exactly two
trusted-host publication commits: all final `TEST_PATHS` changes first, then
all remaining application changes. Unsafe path policies, empty strata,
symlinks, submodules, and tree drift refuse export. Apply the mailbox with
`git am` to a fresh isolated product branch. The flattened patch remains
diagnostic compatibility output and must not be committed as one product
change. Refresh and reverify the resulting branch against current protected
main. When successful siblings finish in different retained lanes,
`product-export --tickets T-NNN,...` exports only the named completed subset
while applying the same role-evidence and Reviewer checks.
The default output remains the new `export/` directory at the lane root. After
that target is consumed, a later resumed sibling may use `--output` with a
different new owner-only directory strictly inside the lane root. Sensitive
lane subtrees, symlinks, existing targets, and outside paths are refused; a
failed export removes only the output claim it created.

Reviewer reconciliation uses the invoking trusted controller after validating
the retained lane pin. For Cursor streams, one successful terminal result may
repeat its single verdict-bearing assistant event; the controller accepts only
an exact embedded assistant result and rejects multiple assistants,
contradictions, duplicate repair ownership, or an unbound result.

## Real Cursor lifecycle

The real probe is an explicit release gate. Put an authenticated Cursor `agent` binary on `PATH`:

```bash
bash scripts/factory-dev-lane.sh cursor-plan

bash scripts/factory-dev-lane.sh cursor-run \
  --root <root-from-plan> --approve-hash <hash-from-plan>
```

`cursor-plan` gives only Cursor subprocesses the normal session home. Current Cursor builds cannot read their authenticated Keychain session when nested under macOS Seatbelt, so the real lane enables Cursor's own `--sandbox enabled` boundary instead; mock mode retains the stricter outer Seatbelt boundary. The synthetic product also denies agent-initiated `Shell(security)`. The one-use approval binds both session files, the lane nonce, factory and product trees, route plan, Cursor version, resolved executable path, and executable bytes. Cursor's hardcoded `/tmp/.cursor` scratch path is redirected through an ephemeral symlink into the lane and removed on every invocation exit. Before claiming that path, the controller may remove a prior empty directory tree only when every entry is owner-owned and not group/other-writable and all subscription providers are idle; files, symlinks, content, unsafe modes, and active providers refuse. The lane then claims the path with one atomic symlink creation. `cursor-run` consumes the approval before provider execution and stops on drift. The reviewer must stay read-only and report `APPROVE`; the final state remains `AWAIT-OPERATOR`.

## Cleanup and boundaries

```bash
bash scripts/factory-dev-lane.sh clean --root <root>
```

Cleanup accepts only the original owner-only lane directory beneath its creation `TMPDIR`, with an unchanged owner, inode, device, marker, and permissions. Failed runs are retained for diagnosis.

Before any isolated attempt, every runtime input is checked lexically and physically beneath the validated lane root and against the production denylist. The lane refuses canonical Nysa, production factory, production Hermes, and LaunchAgent paths. It creates no production receipt or activation record, has no Linear or GitHub integration, and cannot become a production release. The normal protected-main CI, sealed installation, live Cursor canary, product certification, registration, activation, and legacy serialized-provider path remain unchanged.
