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
- Do: nothing is broken on your side. If agents are scheduled, run `scripts/kill-switch.sh` (it creates the `factory/KILL` stop file); otherwise just wait it out. Work already merged is unaffected.
- Don't: swap a role to the other model family to keep moving — the cross-family separation is a quality control, not a convenience.

## Duplicate reviewer row

- Notice: `next-stage.sh` refuses because successful reviewer runs outnumber verdicts, and the extra row came from an overlapping duplicate rather than a real review round.
- Do: count successful reviewer rows for that ticket from oldest to newest. Add `OPERATOR NOTE: reviewer run <N> void — duplicate` to the ticket, using the duplicate row's one-based number. Run `next-stage.sh` again. The next reviewer round number comes from recorded verdicts, so the void row does not renumber it.
- Don't: invent a verdict for the duplicate row or delete ledger history.

## Linear, GitHub, or Railway down

- Do: the factory pauses; nothing needs saving. Board state is in Linear's cloud, code is in GitHub, deploys are in Railway — each recovers on its own. If Linear is down and something is urgent, write decisions in a dated note file and transcribe to tickets after.

## Broken connector (external sends failing)

- Notice: tickets with the `external` label fail their sends; receipts/error comments show it.
- Do: confirm the connector's sandbox/production mode and its credentials in the product's settings. Flip nothing to production while debugging. Escalated failures are a ticket for the factory, not a manual workaround.

## Restore from backup

- Postgres (staging): Railway dashboard → database → Backups → restore. Staging data is disposable; fixtures re-seed it.
- Board: Linear is the source of truth and is cloud-hosted; the ledger CSV is in the product repo and versioned with it.

## Preflight failed before launch

- Notice: the dispatcher escalates with `PREFLIGHT FAIL` output from `scripts/preflight.sh` — adapter contract, version pin, budget headroom, git state, or ticket not Ready.
- Do: read each FAIL line. Common fixes: run `scripts/adapters/contract-test.sh` after a CLI upgrade and set matching `CLAUDE_CODE_PINNED` / `CODEX_PINNED` in `~/.factory/global.env`; raise `DAILY_CAP_USD` or `GLOBAL_DAILY_CAP_USD` if the projected ticket reserve no longer fits; clean and sync the repo to `main`; confirm the ticket is `State: Ready`. Re-run preflight yourself to verify, then tell the dispatcher to resume.
- Don't: tell the dispatcher to launch anyway — every FAIL is predictable at kickoff and will block mid-pipeline.

## Close-out ledger PR

- Notice: at ticket close-out the dispatcher opens a short-lived bookkeeping PR (e.g. `bookkeeping/T-NNN-closeout`) carrying new ledger rows and run logs.
- Do: review and merge it like any factory bookkeeping change — this is the sanctioned ledger write path, not a controls violation.
- Don't: ask the dispatcher to commit ledger rows directly to `main` or to a ticket branch outside this flow.

## Test commit order before operator review

- Notice: reviewer approved but CI fails the test-immutability gate because test commits came after implementation.
- Do: ensure `scripts/reorder-test-fixes.sh` is present (branch `kit/reorder-test-fixes` in the kit repo). The dispatcher runs it at AWAIT-OPERATOR before opening the PR. If the script is missing, merge or cherry-pick it from that branch first.
- Don't: waive the immutability gate or ask the builder to edit tests post-implementation.

## Authoring epics and big tickets (operator-side tools, not factory stages)

These run in your interactive session — never inside the loop. The factory's own spec quality gate is the spec-linter stage; these tools raise the quality of what you feed it.

**spec-kit, pinned install.** `specify` v0.12.11 is installed via `uv tool install specify-cli --from git+https://github.com/github/spec-kit.git@v0.12.11`; the authoring workspace is `~/Projects/spec-authoring` (claude integration, `.specify/` + `.claude/commands/`). Flow for an epic: `/speckit-specify` (write the spec from an intent) → `/speckit-clarify` (structured de-risking questions) → `/speckit-checklist` (requirements-quality checklist) — then carve the result into factory tickets by hand, or use `/speckit-taskstoissues` to seed the board. Do not use `/speckit-implement`, `/speckit-plan`, or `/speckit-tasks` to produce factory artifacts — they duplicate the Planner/Builder and skip the factory's gates. Upgrading the pin: bump the `uv tool install` tag, then refresh `vendor/spec-kit/` per its README.

**gstack planning suite (already installed under `~/.claude/skills/gstack/`).** Interactive-only by its own design (its preamble blocks headless runs). Useful for instantiation-scale documents: `/office-hours` and `/plan-ceo-review` to pressure-test scope, `/plan-eng-review` for an engine spec, `/spec` for authoring a single rich ticket. Treat their output as draft input to the Planner, not as a frozen contract — the Planner still owns the ticket and the spec-linter still lints it.

## The general rule

When unsure: kill switch first (it's always safe), read the ledger and the ticket trail second, escalate to a fresh planning session third. Nothing in the factory is made worse by stopping it.
