# Walking skeleton

The mandatory first milestone of any instantiation. One deliberately trivial feature pushed through every layer, proving the pipeline before any backlog exists.

## Why it exists

The scaffold (repo layout, CI, deploy, database wiring) is the one artifact no agent process self-verifies and a non-technical operator cannot debug. If it's subtly broken, every later ticket inherits the breakage. The skeleton forces the discovery to happen on the cheapest possible feature.

## What it must prove, end to end

1. Frontend page calls a backend endpoint.
2. The backend writes to and reads from the database.
3. Tests for the feature run in CI and pass.
4. The PR gets a preview deploy; merge deploys to staging.
5. The Narrator posts an evidence bundle; the operator opens the staging URL and sees the feature work.

A good skeleton feature: create one record via a form, list it back. Nothing more.

## The gate

The operator clicking a working staging URL is the acceptance test. Until that click happens, no tickets are written, no backlog is cut, no other work proceeds.
