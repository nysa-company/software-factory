# Operator runbook

What to do when something breaks, written for a non-technical operator. Each entry: how you notice, what to do, what not to do.

## Stuck ticket (no movement for hours)

- Notice: ticket sits In progress with no new commits or comments.
- Do: check the terminal/session running the role. If it's spinning or confused, stop it, add a ticket comment "run abandoned — restarting", and re-run the role via `run-agent.sh`. Second stall on the same ticket → move it to Blocked-Escalated and re-read the ticket's contract: stalls usually mean the spec is ambiguous.
- Don't: let a stuck run keep burning budget while you wait.

## Runaway spend

- Notice: daily spend rollup jumps, or a provider console alert fires.
- Do: run `scripts/kill-switch.sh` immediately (safe — it stops, it doesn't break anything). Read `factory/ledger.csv` for today's rows and find the expensive role/ticket. That ticket goes to Blocked-Escalated; resume the rest by removing `factory/KILL`.
- Don't: rotate API keys for a spend problem — that's for leaks, and it kills your own sessions too.

## Failed deploy / broken staging

- Notice: staging URL errors, or a Narrator bundle says "preview broken".
- Do: check Railway's dashboard for the failing deploy log; the usual fix is reverting the last merged PR per `ci/rollback-drill.md`. If staging is down but no recent merge happened, restart the Railway service from its dashboard.
- Don't: approve anything while staging is broken — bundles can't be verified.

## Leaked secret (a key appears in a file, log, or commit)

- Notice: a scanner alert, a reviewer comment, or you see a key string somewhere it shouldn't be.
- Do: this is the one case for rotation. Revoke the exposed key in the provider console, issue a new one, update GitHub/Railway secrets. If it reached git history, treat the repo history as public: rotate everything that repo ever saw. Then file a ticket to fix the path it leaked through.
- Don't: just delete the file and move on — the key is still burned.

## Model provider down (Claude or OpenAI outage)

- Notice: runs fail immediately with API errors; provider status page confirms.
- Do: nothing is broken on your side. Stop starting runs (drop the KILL file if agents are scheduled), wait it out. Work already merged is unaffected.
- Don't: swap a role to the other model family to keep moving — the cross-family separation is a quality control, not a convenience.

## Linear, GitHub, or Railway down

- Do: the factory pauses; nothing needs saving. Board state is in Linear's cloud, code is in GitHub, deploys are in Railway — each recovers on its own. If Linear is down and something is urgent, write decisions in a dated note file and transcribe to tickets after.

## Broken connector (external sends failing)

- Notice: tickets with the `external` label fail their sends; receipts/error comments show it.
- Do: confirm the connector's sandbox/production mode and its credentials in the product's settings. Flip nothing to production while debugging. Escalated failures are a ticket for the factory, not a manual workaround.

## Restore from backup

- Postgres (staging): Railway dashboard → database → Backups → restore. Staging data is disposable; fixtures re-seed it.
- Board: Linear is the source of truth and is cloud-hosted; the ledger CSV is in the product repo and versioned with it.

## The general rule

When unsure: kill switch first (it's always safe), read the ledger and the ticket trail second, escalate to a fresh planning session third. Nothing in the factory is made worse by stopping it.
