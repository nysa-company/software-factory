# Operating envelope — <PRODUCT NAME>

Hard limits for the factory. The run wrapper and provider console caps enforce these numbers; this document records them and what happens at each threshold. Agents never see or modify this file's limits at runtime.

## Budgets

| Limit | Value | Enforced by |
|---|---|---|
| Per-run budget (USD) | $<FILL: e.g. 5> | wrapper reservation; adapter hard stop where supported (`--max-budget-usd` for Claude Code) |
| Per-ticket budget (USD), all runs summed | $<FILL: e.g. 15> | run wrapper ledger check (`PER_TICKET_BUDGET_USD`) |
| Per-run max turns | <FILL: e.g. 60> | logged; the dollar budget is the hard stop |
| Per-run wall-clock cap | <FILL: e.g. 45 min> | run wrapper timeout |
| Daily factory cap (USD) | $<FILL: e.g. 75> | run wrapper ledger check + provider console caps |
| Monthly cap (USD) | $<FILL> | provider console caps |

Cap checks reserve the new run's full per-run budget before starting (a run can't start at $74.99 of a $75 cap), and unparsable run costs keep that conservative reservation in the ledger rather than logging $0. Cursor CLI has no documented per-run dollar stop, so its full reservation always remains; approved telemetry may add an observational estimate only.

When the daily cap is hit the run wrapper refuses to start new runs until the next day. The console caps are the backstop if the wrapper is bypassed or broken.

## Retries and escalation

| Rule | Value |
|---|---|
| Reviewer rounds per ticket | 2, then ticket → Blocked-Escalated with a plain-language Narrator note |
| Builder attempts per ticket | <FILL: e.g. 2 full runs>, then Blocked-Escalated |
| Tickets a single defect can reopen | 1 — a second reopen means Blocked-Escalated |
| Blocked-Escalated response | Operator decides: re-spec, split, or drop. Agents never self-unblock. |

## External actions

- All external sends are sandboxed (allowlisted recipients / mock endpoints) until the operator explicitly flips production mode per connector.
- Any ticket whose change can send externally is tagged `external` and risk-sorted first in review; its evidence bundle follows the `external send` row of the evidence rubric.

## Exit thresholds (gate any increase in autonomy)

Automation, concurrency, or a dispatcher may only be added when a pilot of <FILL: e.g. 20> tickets shows:

| Metric | Threshold |
|---|---|
| Cost per merged ticket | ≤ $<FILL> |
| Escaped defects (bug tickets linked to Done tickets) | ≤ <FILL: e.g. 10%> |
| Tickets hitting Blocked-Escalated | ≤ <FILL: e.g. 15%> |
| Operator minutes per ticket | ≤ <FILL: e.g. 15> |

## Kill switch

`scripts/kill-switch.sh` stops all running sessions and disables scheduled jobs. It does not rotate keys — key rotation is incident response for a suspected leak, per the operator runbook. Console caps remain active regardless.
