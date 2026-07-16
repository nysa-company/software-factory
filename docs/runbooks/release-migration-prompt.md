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

Follow docs/runbooks/operator.md § "Preparing and activating a release"
exactly, in order:

1. Confirm the full SHA is on origin/main and the required aggregate `ci`
   check passed on Linux and macOS.
2. bash scripts/factory-kit.sh install --repo {{KIT_CHECKOUT_PATH}} --sha
   {{SHA}}, then certify against the product. Record the receipt ID and
   expiry in your report.
3. {{CANARY_LINE: "Run the real-Hermes canary with a separate sandbox product
   and profile — MANDATORY for this release because it changes a
   compatibility-sensitive surface ({{SURFACES_TOUCHED}}). Never copy the
   production .env, secrets, board mapping, registry, ledger, or LaunchAgent
   into the sandbox." | "No compatibility-sensitive surface changed between
   {{LAST_ACTIVATED_SHA}} and {{SHA}}; the canary may be skipped."}}
4. Confirm no active runs and no nonterminal ticket with a different Kit-SHA.
5. Open the product PR that changes ONLY factory/KIT_PIN to the full SHA.
   Stop and wait for my approval before merging it. After merge, verify the
   product tree still matches the certification receipt.
6. Publish maintenance with factory-kit.sh pause; wait for runs and all
   dispatcher leases to drain.
7. factory-kit.sh plan — it must report "No files were changed." If not, stop.
8. Stop only the product factory profile and reconciler (leave the dashboard
   and primary Hermes profile running).
9. factory-kit.sh activate, restart the factory services, then collect doctor
   JSON, sandbox smoke, PID, Linear freshness, and repeated health probes.
10. Remove MAINTENANCE only after every acceptance check passes.

Then prove the release works by running two real tickets end to end:

11. Select two Ready tickets that are low or medium risk, have `External: no`,
    and have no overlapping file ownership. If none are Ready, stop and ask me
    to stage two.
12. Run each ticket's full lifecycle through the trusted launcher
    (~/.factory/bin/factory-launch {{PROJECT_SLUG}} run) to the contract's
    Review/evidence-bundle boundary — planner, spec-linter, test-author,
    builder, CI, reviewer, narrator. Do not merge; contract 1.2 stops at the
    documented evidence gate.
13. Concurrency: run them as two concurrent leases ONLY if the product's
    MAX_CONCURRENT_TICKETS is 2 AND a current operator waiver covers this
    pair (check factory/ for the dated authorization record). Otherwise run
    them sequentially — never grant yourself the waiver.
14. Acceptance evidence per ticket: every role launch accepted by the
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
from step 14, and any deviations.
```
