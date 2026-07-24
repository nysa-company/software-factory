# Review evidence rubric

What the operator must see in the Narrator's bundle before approving, by risk class. The class is set by the planner via labels; when in doubt, the higher class applies.

## Risk classes

| Class | Definition | Bundle must include |
|---|---|---|
| Internal change | Behavior visible only inside the app; no external side effects, no schema change | Summary, preview link and screenshots when the frozen contract has a visual/deployable surface, criteria table, cost, revert one-liner |
| Schema change | Touches the database schema or stored-data shape | All of the above, plus: the migration description in plain language, what happens to existing data, confirmation the migration ran on preview, and the tested rollback path for the migration itself |
| External send | Can deliver anything outside the system (email, API write to a third-party tool, webhook) | All of internal, plus: exact destination (who receives what, when), whether it fires once or recurringly, sandbox/production status of the connector, and — if irreversible — an explicit "cannot be undone once live" warning replacing the revert line |

## Rules

- A bundle missing any required row is not approvable; the operator's correct move is "send back", not benefit of the doubt.
- Failed criteria are shown first, never buried.
- External-send tickets are risk-sorted first whenever the operator reviews a batch.
- Screenshots show the actual preview deploy, never mockups. Where a design reference exists (e.g. a prototype page), include the side-by-side.
- In a trusted `FACTORY_DEV_PRLESS_EVIDENCE_V1` development proof, when the
  frozen contract explicitly has no browser, HTTP, or deployable surface, the
  Preview and Screenshots sections say `Not applicable — backend-only
  contract` and point to focused verification. Production bundles still
  require their normal exact PR and preview evidence.
