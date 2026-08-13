# Documentation map

Durable product and operating truth lives under `docs/`:

- [product-brief.md](product-brief.md) — purpose, audience, scope, and invariants.
- [architecture.md](architecture.md) — engine boundaries, role flow, budgets, and trust model.
- [model-routing.md](model-routing.md) — primary and secondary routes, profiles, mid-ticket fallback, family separation, and operator commands.
- [factory-setup.md](factory-setup.md) — product-instantiation checklist and validator.
- [factory-runtime.md](factory-runtime.md) — immutable kit releases, public Factory contract, native scheduling, certification, qualification, activation, recovery, rollback, and retention.
- [factory-contract-changelog.md](factory-contract-changelog.md) — public contract history and migration notes.
- [factory-development-lane.md](factory-development-lane.md) — isolated mock and explicitly approved Cursor lifecycle for fast factory iteration.
- [git-flow.md](git-flow.md) — branch, PR, merge, and protection policy.
- [ai-review.md](ai-review.md) — local pre-publication AI review rubric.
- [workflows/](workflows/) — ticket and evidence workflows.
- [runbooks/](runbooks/) — operator recovery procedures.
- [runbooks/factory-continuous-improvement-session-prompt.md](runbooks/factory-continuous-improvement-session-prompt.md) — reusable evidence-first prompt for improving deterministic four-ticket delivery without changing a frozen candidate.
- [operations/](operations/) — hosting, rollback, and walking-skeleton guidance.
- [metrics.md](metrics.md) — factory measurement schema.

Functional Markdown stays beside what consumes it: executable prompts in `roles/`, copied templates in `envelope/`, conformance fixtures and evidence in `conformance/`, and vendored material in `vendor/`. Root control files, `context/memory.md`, GitHub templates, future `templates/`, and a real `deploy/README.md` are also valid exceptions.

Route new information from disposable `.context/` notes to `context/memory.md`, then promote stable truth here, startup-critical rules to `AGENTS.md`, and deferred work to `TODOS.md`. Do not duplicate the same source of truth.
