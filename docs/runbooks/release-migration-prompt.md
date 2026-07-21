# Release-migration prompt template

Copy the block below into a fresh agent session to perform a kit release and
prove it works. Fill every `{{...}}` placeholder first. One release can batch
any number of merged PRs — point `{{SHA}}` at the `main` tip you want to ship.

Before sending, decide the canary line: run
`git diff --name-only {{LAST_ACTIVATED_SHA}}..{{SHA}}` and compare against
`compatibility_sensitive_surfaces` in `integrations/hermes/contract.json`.
Any hit makes the real-Hermes canary mandatory; otherwise it may be skipped.

Keep in mind when batching: rollback restores the whole previous generation
(all batched changes revert together), and changes you want to measure in the
next evidence window (e.g. model tiering) attribute more cleanly when released
alone.

---

```
Perform the software-factory kit release for candidate SHA {{SHA}}
({{SHORT_DESCRIPTION}}, PRs: {{PR_LIST}}) and activate it for the
{{PROJECT_SLUG}} product (product repo: {{PRODUCT_REPO_ABSOLUTE_PATH}}).

The execution computer is {{EXECUTION_HOST}} and this is an
{{HOST_MODE: "active in-place" | "inactive replacement"}} migration. The
approved Nysa Agents plugin version is {{NYSA_AGENTS_PLUGIN_VERSION}}.
The approved CLI versions are Codex {{CODEX_CLI_VERSION}}, Claude Code
{{CLAUDE_CODE_CLI_VERSION}}, and Cursor Agent {{CURSOR_AGENT_VERSION}}.

Follow docs/runbooks/operator.md § "Preparing and activating a release"
exactly, in order:

1. Confirm the full SHA is on origin/main and the required aggregate `ci`
   check passed on Linux and macOS.
2. Inventory every nonterminal ticket and its committed Kit-SHA. Finish each
   on its current release or prepare the protected-main
   `factory/migrations/inflight-release/{{SHA}}.json` authorization with the
   exact repository, source/target SHAs, sorted ticket branch heads, and states.
   Each authorized head must retain its old-kit v1 route plan. Do not migrate a
   pin, route journal, or lease yet.
3. If this is the active in-place host, stop new dispatch, publish maintenance,
   and drain all runs and dispatcher leases before changing user-scoped tools.
   If it is an inactive replacement, keep its dispatcher, reconciler, and
   LaunchAgent disabled.
4. Require the candidate machine's `~/.factory/global.env` to pin
   `CODEX_PINNED={{CODEX_CLI_VERSION}}`,
   `CLAUDE_CODE_PINNED={{CLAUDE_CODE_CLI_VERSION}}`, and
   `CURSOR_AGENT_VERSION={{CURSOR_AGENT_VERSION}}`. Install those exact CLI
   versions through the normal controlled-path mechanism. Verify
   `codex --version`, `claude --version`, `agent --version`, the physical
   targets under `~/.factory/bin`, and
   `scripts/adapters/contract-test.sh --routes`. Stop on any missing pin or
   mismatch. Do not copy credentials from another computer.
5. Upgrade and verify Nysa Agents on this execution computer:
   - `codex plugin marketplace upgrade nysa-agents-plugin`, then require
     `codex plugin list` to show Nysa Agents enabled at
     {{NYSA_AGENTS_PLUGIN_VERSION}}.
   - `claude plugin marketplace update nysa-agents-plugin`, then
     `claude plugin update nysa-agents@nysa-agents-plugin`; require
     `claude plugin list` to show {{NYSA_AGENTS_PLUGIN_VERSION}} and retain
     marketplace `autoUpdate: true`.
   - Restart Codex and Claude sessions so no old process keeps old plugin code.
   Run the plugin's repository-baseline plan. If it proposes tracked changes,
   stop: that is a separate product change to review before certification.
6. Verify Node 22 and the product certification dependencies. For nysa-app,
   require PostgreSQL to be reachable at `127.0.0.1:55432` before certifying.
7. Run `bash scripts/factory-kit.sh install --repo {{KIT_CHECKOUT_PATH}} --sha
   {{SHA}}`, then certify against the product. These commands must reuse the
   exact successful main GitHub run and perform only local smoke for the kit;
   never substitute a local factory full suite. Record the host-bound receipt
   ID and expiry. A receipt from another computer is invalid here.
8. {{CANARY_LINE: "Run the real-Hermes canary with a separate sandbox product
   and profile — MANDATORY for this release because it changes a
   compatibility-sensitive surface ({{SURFACES_TOUCHED}}). Never copy the
   production .env, secrets, board mapping, registry, ledger, or LaunchAgent
   into the sandbox." | "No compatibility-sensitive surface changed between
   {{LAST_ACTIVATED_SHA}} and {{SHA}}; the canary may be skipped."}}
9. Confirm no active runs, no dispatcher leases, and no unauthorized
   nonterminal ticket with a different Kit-SHA.
10. Open the product PR that changes `factory/KIT_PIN` to the full SHA and, only
   when step 2 identified in-flight tickets, adds the exact authorization file.
   Stop and wait for my approval before merging it. After merge, verify the
   product tree still matches the certification receipt.
11. For a replacement-host cutover, publish maintenance on the old host and
    drain it now. Confirm its dispatcher is stopped; if that cannot be proven,
    revoke its provider and Linear execution access before enabling this host.
12. factory-kit.sh plan — it must report "No files were changed." If not, stop.
13. Stop only the product factory profile and reconciler (leave the dashboard
   and primary Hermes profile running).
14. factory-kit.sh activate, restart the factory services, then collect doctor
   JSON, sandbox smoke, PID, Linear freshness, and repeated health probes.
15. For authorized in-flight tickets, keep maintenance while reviewing every
   sealed `models migrate-plan` preview. Remove maintenance before applying
   only the exact operator-approved `models migrate`, then claim fresh leases.
   With no in-flight tickets, remove maintenance only after every acceptance
   check passes.

Then prove the release works by running two real tickets end to end:

16. Select two Ready tickets that are low or medium risk, have `External: no`,
    and have no overlapping file ownership. If none are Ready, stop and ask me
    to stage two.
17. Run each ticket's full lifecycle through the trusted launcher
    (~/.factory/bin/factory-launch {{PROJECT_SLUG}} run) to the contract's
    Review/evidence-bundle boundary — planner, spec-linter, test-author,
    builder, CI, reviewer, narrator. Do not merge; contract 1.2 stops at the
    documented evidence gate.
18. Concurrency: use no more than the product's configured
    MAX_CONCURRENT_TICKETS and select tickets with non-overlapping ownership.
    Never invent a capacity override or authorization.
19. Acceptance evidence per ticket: every role launch accepted by the
    sequencer under the new Kit-SHA, ledger and manifest rows consistent
    (reservation ≤ per-run budget, settled cost recorded), reviewer verdict
    recorded, evidence bundle posted, doctor healthy afterward.

Hard rules: any launcher/wrapper refusal, doctor warning, or plan drift is a
stop-and-report, never a workaround. If anything fails after activation, keep
MAINTENANCE, run factory-kit.sh reconcile, and follow its terminal result —
do not hand-edit active.json or the journal. Never print secrets; redact
values whose key matches key|token|secret|password|url|dsn|conn|auth.

Report at the end: receipt ID + expiry, canary result (or the skip
justification), activation journal entry, doctor summary, per-ticket evidence
from step 19, and any deviations.
```
