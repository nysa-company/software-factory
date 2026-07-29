# Product brief

## Product

Software Factory Kit is product-agnostic infrastructure for running an AI software factory. Agents plan, test, build, review, and document work while a human sets priorities and approves from evidence bundles rather than raw diffs.

## Audience

The primary users are an operator instantiating the kit for a product and the agents working through its ticket lifecycle. Product-specific knowledge and runtime state stay in each product repository.

## Current scope

The kit provides role contracts, budgeted CLI adapters, ticket sequencing, CI enforcement, evidence requirements, operator runbooks, metrics, and a permanent Relay conformance product. It does not own product code, product documentation, hosting accounts, credentials, or a product backlog.

## Invariants

1. Budgets are enforced by the run wrapper and provider caps; agents cannot raise them.
2. Tests are authored before implementation by a different model family, and builders cannot edit them.
3. External actions remain sandboxed or explicitly allowlisted until production approval.
4. CI and an independent reviewer verify the frozen contract before merge.
5. A human approves from a Narrator evidence bundle; lifecycle repair continues
   while budget remains and every transition retains valid role evidence.

See [architecture.md](architecture.md) for the engine model and [factory-setup.md](factory-setup.md) to instantiate it.
