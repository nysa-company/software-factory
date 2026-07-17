# Deferred work

Activate these only after the first product instantiation has a deployed web application. The source decision was recorded on 2026-07-13 in the Nysa product evaluation of spec-kit and gstack.

- Pilot a report-only browser verifier between Reviewer approval and Narrator, then add a standing role only if the pilot succeeds.
- Add an operator-triggered periodic security audit.
- Add post-deploy canary monitoring.
- Add performance baselines once a real deployment exists.
- Add a code-quality score only when it informs a real release decision.
- Add milestone spec-drift checks when a living deployed specification exists.

## Concurrency-pilot follow-ups

Prioritize these from the first two-ticket contract 1.2 pilot:

- Add machine-readable certification progress and a `--watch` view with phase names, elapsed time, and the current deterministic gate.
- Reuse still-valid kit-suite evidence for an unchanged sealed SHA; always rerun product-tree checks and receipt binding, and invalidate reuse when the host, release tree, suite definition, or evidence lifetime changes.
- Add a maintenance-only accounting audit and conservative reconciliation command for legacy manifests, with quarantine evidence and no guessed cost reduction.
- Add a trusted operator-approved lease-release route for tickets stopped in Review, without exposing or persisting opaque lease IDs.
- Provide forge-neutral merge-queue or auto-merge guidance so protected changes merge after all required checks pass without operator polling.
- Extend preflight with Git forge/API health, required local services, provider CLI authentication, and configured deployment-check readiness.
- Pilot provider-call concurrency only after another successful two-lease run; retain the serialized provider and global-ledger locks until bounded parallel accounting and crash recovery are proven.

## Open-source evaluation follow-ups

- Pilot one pinned SWE-ReX local-container backend for a non-production role behind `factory-launch`; keep budgets, timeouts, manifests, commits, pushes, protected-test checks, sequencing, and Linear updates outside it, and prove kill, crash, telemetry-loss, and mutation behavior before product use.
- Compare E2B or Daytona with the same canary only if the local-container pilot cannot close the documented same-UID isolation gap.
- When implementing trusted approval and close-out, evaluate Flow-Next-style requirement-to-evidence traceability and in-toto/SLSA-shaped provenance fields before inventing a factory-specific evidence schema.
- Reuse Open SWE-style deterministic trigger IDs and short-lived GitHub credentials in the Hermes supervisor only where they preserve Linear and factory authority; adopt metaswarm review rubrics only when an observed review gap justifies them.
