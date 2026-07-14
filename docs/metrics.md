# Factory metrics

What gets measured per ticket, where it comes from, and what it gates. Weekly reporting tooling is built after the first shakedown, from observed needs — this schema is fixed now so the data exists from ticket 1.

## Per-ticket record

| Field | Source |
|---|---|
| ticket id | Markdown filename; Linear identifier stored in `linear-map.json` |
| cost_usd | sum of ledger rows for the ticket (`factory/ledger.csv`) |
| attempts | count of builder runs in the ledger |
| review_rounds | reviewer comments on the PR (0–2) |
| cycle_time | reconciled Linear Ready timestamp → factory Done log timestamp |
| operator_minutes | operator self-report in the Linear approval comment |
| escalated | did the ticket ever enter Blocked-Escalated (boolean + reason) |
| escaped_defect | a later bug ticket links back to this Done ticket (boolean, retroactive) |
| external | Markdown `External:` field projected as the Linear `external` label |
| prompt versions | logged per run in the ledger |
| run identity | `run_id` joins the ledger row to its atomic `factory/runs/<run_id>.meta` manifest |
| backend provenance | adapter, provider family, exact model ID, selection reason, and adapter compatibility version from trailing ledger columns |
| cost basis | provider-reported, token estimate, dated Cursor pricing estimate, test fixture, or conservative full-budget reservation |

## Definitions that keep the numbers honest

- **Escaped defect** is mechanical: a bug ticket with a "caused by" link to a Done ticket. No link, no defect count — so linking bugs back is mandatory triage hygiene.
- **Cost per merged ticket** includes failed attempts and review runs — the whole ticket's ledger sum, not just the winning run.
- **Cursor cost** keeps the full per-run reservation in the ledger. When the approved CLI emits token usage and a dated pricing snapshot is configured, an observational estimate is added to the local run output but never reduces the reservation; the Cursor dashboard is authoritative.
- **Fallback provenance** is interpreted from the row itself, never from today's backend matrix. The first nine ledger columns remain stable for sequencer compatibility; provenance fields are append-only.
- **Operator minutes** counts everything: reading the bundle, clicking the preview, deciding. It is the number that answers "is this factory actually saving me time."

## What the numbers gate

The envelope's exit thresholds reference these fields. Autonomy only increases (dispatcher, concurrency, product scout) when a pilot's aggregate numbers clear the thresholds — the trend line is the factory's own QA.
