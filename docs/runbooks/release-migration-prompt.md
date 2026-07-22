# Release-migration prompt template

Copy the block below into a fresh agent session to release one protected-main
kit SHA and prove it on one product. Fill every `{{...}}` placeholder first.

Before sending, run
`git diff --name-only {{LAST_ACTIVATED_SHA}}..{{SHA}}` and compare it with
`compatibility_sensitive_surfaces` in `integrations/hermes/contract.json`.
Any hit makes the isolated real-Hermes canary mandatory.

---

```
Perform the software-factory release for candidate {{SHA}}
({{SHORT_DESCRIPTION}}, PRs: {{PR_LIST}}) and activate it for
{{PROJECT_SLUG}} at {{PRODUCT_REPO_ABSOLUTE_PATH}}.

Execution host: {{EXECUTION_HOST}}
Host mode: {{HOST_MODE: "active in-place" | "inactive replacement"}}
Nysa Agents plugin: {{NYSA_AGENTS_PLUGIN_VERSION}}
Codex CLI: {{CODEX_CLI_VERSION}}
Claude Code CLI: {{CLAUDE_CODE_CLI_VERSION}}
Cursor Agent CLI: {{CURSOR_AGENT_VERSION}}
Model profile: {{MODEL_PROFILE_ID}} (use cursor-balanced-v2 for Nysa)
Per-ticket budget: USD {{PER_TICKET_BUDGET_USD}} (use 100.00 for Nysa)

Follow docs/runbooks/operator.md § "Preparing and activating a release" in
this exact order:

1. Fetch the kit repository. Require {{SHA}} to be the exact current
   `origin/main` commit and require the authenticated GitHub Actions push run
   for that SHA to have all three Linux shards, all three macOS shards, the
   aggregate `ci` job, and `test-immutability` successful. A PR run, partial
   shard set, local full suite, or evidence for another SHA is not acceptable.
2. Install the sealed candidate with `bash scripts/factory-kit.sh install
   --repo {{KIT_CHECKOUT_PATH}} --sha {{SHA}}`. Installation must reuse the
   exact protected-main evidence from step 1 and run only the local sandboxed
   host smoke; never substitute the complete local factory suite.
3. Inventory all runs, claims, leases, activation journals, and ticket states.
   On an active host, stop new dispatch, publish maintenance, and drain all
   runs and leases. On an inactive replacement, keep dispatcher, reconciler,
   and LaunchAgent disabled. Do not alter an in-flight route plan or lease.
4. Prepare one clean canonical product checkout at
   {{PRODUCT_REPO_ABSOLUTE_PATH}} from the latest `origin/main`, on a dedicated
   migration branch. Before certification, commit the exact candidate
   `factory/KIT_PIN`, matching `PER_TICKET_BUDGET_USD={{PER_TICKET_BUDGET_USD}}`
   in both envelope files, and any migration evidence required below. Do not
   certify a dirty tree, a different path, or a worktree whose tree differs
   from the proposed product PR.
5. For the Nysa migration, create one reviewed request and run
   `scripts/protected-merge-reconciliation.py --product
   {{PRODUCT_REPO_ABSOLUTE_PATH}} --request <reviewed-request.json>`. Require
   one authorization, the complete exact receipt batch for T-024, T-030, and
   T-031, their Done/Migration ticket projections, and the new `KIT_PIN` in
   the same product commit. Bind the already-reviewed T-032 ticket and ruling
   as exact companion path/blob entries; any other companion is forbidden.
   T-024 uses `reviewed-clean-history-adoption`;
   T-030 and T-031 use `merged-adoption`. Preserve authentic approval evidence
   exactly as required by the source state. Bind each ticket's immutable
   `evidence_head`; for T-024 require its ancestry into the original reviewed
   PR and exact product/test blob equality through the clean-history adoption.
   This batch is not an ordinary
   in-flight migration, normal Done closeout, or permission to synthesize
   attestations. Any partial batch, changed protected basis, or hand edit
   requires regeneration.
6. Pin and install the approved CLIs on this execution computer before
   certification. Require `~/.factory/global.env` to contain the approved
   `CODEX_PINNED`, `CLAUDE_CODE_PINNED`, and `CURSOR_AGENT_VERSION`; verify
   `codex --version`, `claude --version`, `agent --version`, the physical
   controlled-path targets, and `scripts/adapters/contract-test.sh --routes`.
   Stop on any mismatch. Never copy credentials from another computer.
7. Upgrade Nysa Agents before certification:
   - Upgrade the Codex marketplace plugin and require `codex plugin list` to
     show Nysa Agents enabled at {{NYSA_AGENTS_PLUGIN_VERSION}}.
   - Update the Claude marketplace and plugin and require `claude plugin list`
     to show {{NYSA_AGENTS_PLUGIN_VERSION}} with marketplace auto-update kept.
   - Restart Codex and Claude sessions. Run the repository-baseline plan; any
     proposed tracked baseline change is a separate product change and stops
     this migration.
8. Verify Node 22 and product certification dependencies. For nysa-app,
   require PostgreSQL at `127.0.0.1:55432`, review its pinned dependency fetch,
   and certify the exact clean committed tree at
   {{PRODUCT_REPO_ABSOLUTE_PATH}} with
   `FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1 bash
   scripts/factory-kit.sh certify --project {{PROJECT_SLUG}} --product
   {{PRODUCT_REPO_ABSOLUTE_PATH}} --sha {{SHA}}`. The opt-in applies only to
   this reviewed certification command; installation and unreviewed products
   remain network-denied. Record the host-bound receipt ID and expiry; another
   computer's receipt is invalid.
9. {{CANARY_LINE: "Run the isolated real-Hermes canary with a separate sandbox
   product/profile because this release changes {{SURFACES_TOUCHED}}. Never
   copy production environment, credentials, board mapping, registry, ledger,
   or LaunchAgent into it." | "No compatibility-sensitive surface changed;
   record the exact diff-based skip justification."}}
10. Open the product PR from the already certified migration commit and stop
    for operator approval. Disable auto-merge and bypass for the one-time
    reconciliation batch. After its manual protected merge, fetch canonical
    `origin/main` and require its tracked tree to match the certification
    receipt exactly. Drift requires a new branch, reconciliation basis, and
    certification.
11. On a replacement cutover, now publish maintenance on the old host and
    drain it. Prove its dispatcher and Linear reconciler are stopped; otherwise
    revoke their execution access. Only then transfer the ignored production
    `factory/linear-map.json` securely to the same canonical path with
    owner-only permissions. Never print, commit, or copy it into the canary.
12. Run `factory-kit.sh plan`; require `No files were changed.` Stop only the
    product factory profile and reconciler, leaving the dashboard and primary
    Hermes profile running. Activate while dispatch and Linear remain stopped.
    Collect activation journal, doctor JSON, sandbox smoke, PID, and repeated
    health probes. A failure keeps maintenance published and uses
    `factory-kit.sh reconcile`; never edit `active.json` or the journal.
13. While dispatch remains stopped, preview {{MODEL_PROFILE_ID}}, verify its
    exact six route/model/effort selections, and activate only its returned
    hash with the operator ID. Require envelope inspection to report
    `PER_TICKET_BUDGET_USD={{PER_TICKET_BUDGET_USD}}`. T-024, T-030, and T-031
    must resolve as protected-main Done through the complete reconciliation
    batch; do not run `models migrate`, repin, or ordinary Done closeout for
    them. Start Linear reconciliation, require fresh sync and healthy doctor,
    then remove maintenance and start dispatch.

Prove the release with exactly one real ticket:

14. Run T-032 alone. Require its Backlog ticket and operator ruling on
    protected main, `External: no`, and no overlapping active ownership. Keep
    its contract unfrozen until Planner creates and commits the exact contract
    on `ticket/T-032`; then require the normal Spec-linter gate. Do not claim
    another ticket until T-032 is protected-main Done.
15. Run the complete trusted lifecycle: Planner, Spec-linter, Test-author,
    Builder, exact-head GitHub CI, Reviewer, Narrator, Linear approval,
    protected product merge, post-merge checks, and trusted Done closeout.
    Use only `~/.factory/bin/factory-launch {{PROJECT_SLUG}}`; never bypass a
    refusal or manufacture evidence.
16. Accept the rollout only when T-032 is valid protected-main Done, its ledger
    and manifests reconcile, Linear is fresh, doctor is healthy, and no run,
    claim, or lease remains. Only then may the operator stage up to four
    non-overlapping Ready tickets.

Hard rules: no local factory full CI or AI review; GitHub owns complete factory
verification. Any launcher refusal, doctor warning, mutation drift, incomplete
reconciliation batch, or evidence mismatch is a stop-and-report. Never print
secrets; redact values whose key matches
key|token|secret|password|url|dsn|conn|auth.

Report: protected-main CI run, install proof, receipt ID and expiry, plugin/CLI
version checks, reconciliation batch result, canary result or skip reason,
activation journal, model/envelope activation, doctor summary, T-032 lifecycle
evidence, and deviations.
```
