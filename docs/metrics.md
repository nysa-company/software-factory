# Factory metrics

What gets measured per ticket, where it comes from, and what it gates. Weekly reporting tooling is built after the first shakedown, from observed needs — this schema is fixed now so the data exists from ticket 1.

## Per-ticket record

| Field | Source |
|---|---|
| ticket id | Markdown filename (`T-NNN`) |
| cost_usd | sum of effective rows for the ticket (`factory/runtime-ledger.csv`) |
| attempts | count of builder runs in the ledger |
| review_rounds | reviewer comments on the PR (0–2) |
| cycle_time | ready receipt timestamp → factory Done log timestamp |
| operator_minutes | operator self-report on the ticket log at approval |
| escalated | did the ticket ever enter Blocked-Escalated (boolean + reason) |
| escaped_defect | a later bug ticket links back to this Done ticket (boolean, retroactive) |
| external | Markdown `External:` field |
| prompt versions | logged per run in the ledger |
| run identity | `run_id` joins the ledger row to its atomic `factory/runs/<run_id>.meta` manifest |
| route provenance | pinned catalog/profile/policy hashes plus transport, gateway, inference provider, family, account route, selection ID, reported identity, adapter, effort, and adapter version from the ticket route plan and run manifest |
| cost basis | provider-reported, token estimate, dated Cursor pricing estimate, test fixture, or conservative full-budget reservation |

## Definitions that keep the numbers honest

- **Escaped defect** is mechanical: a bug ticket with a "caused by" link to a Done ticket. No link, no defect count — so linking bugs back is mandatory triage hygiene.
- **Cost per merged ticket** includes failed attempts and review runs — the whole ticket's ledger sum, not just the winning run.
- **Accounting state** comes from each atomic run manifest. Pre-GO failures are `launch_void` at $0; post-GO failures retain reported cost or the full reservation when cost is unknown.
- **Cursor cost** keeps the full per-run reservation in the ledger. When the approved CLI emits token usage and a dated pricing snapshot is configured, an observational estimate is added to the local run output but never reduces the reservation; the Cursor dashboard is authoritative.
- **Route identity** distinguishes the selectable ID sent to a CLI from the identity it reports. Cursor's two exact models are separate routes even though they share a transport adapter.
- **Portfolio provenance** is interpreted from the immutable ticket plan, never from today's active profile. Resolution requires exact routes for all six roles; roles never re-resolve, and a run only re-probes its pinned route.
- **Ledger compatibility** is unchanged by the router. Existing columns and reduction remain authoritative; richer manifest/route-plan provenance can support future provider/family/model budgets, but those limits are not implemented.
- **Subscription quotas** are not authoritative telemetry. Temporary `credits_exhausted` overrides record an operator decision and expiry; they do not prove remaining subscription credit or replace provider-console review.
- **Operator minutes** counts everything: reading the bundle, clicking the preview, deciding. It is the number that answers "is this factory actually saving me time."

## What the numbers gate

The envelope's exit thresholds reference these fields. Autonomy only increases (dispatcher, concurrency, product scout) when a pilot's aggregate numbers clear the thresholds — the trend line is the factory's own QA.
