# Operating envelope — Relay (conformance shakedown)

Filled from the kit template with deliberately tiny limits: the shakedown's job is to hit them.

| Limit | Value | Enforced by |
|---|---|---|
| Per-ticket budget (USD) | $0.50 | run wrapper / `--max-budget-usd` |
| Per-run max turns | 15 | logged (Claude Code 2.1.207 dropped `--max-turns`; budget is the hard stop) |
| Per-run wall-clock cap | 5 min | run wrapper timeout |
| Daily factory cap (USD) | $2.00 | run wrapper ledger check |

Retries/escalation: 2 reviewer rounds; 2 builder attempts; then Blocked-Escalated. External actions: Relay's allowlist is `test@example.com` only — production mode does not exist in this product by design.

Exit thresholds don't apply (this is the kit's own test, not a product pilot).
