# Railway operations

Railway hosts staging, PR preview deploys, and Postgres. Chosen over Fly.io for a non-technical operator: no Dockerfile required, built-in Postgres with automated backups, deploys from GitHub pushes.

## Setup (once per product, ~20 minutes)

1. railway.app → New Project → Deploy from GitHub repo → pick the product repo.
2. Add Postgres: New → Database → PostgreSQL. Railway injects `DATABASE_URL` into the app service.
3. Enable PR environments: Settings → Environments → enable "PR environments" (this is the preview deploy the Narrator screenshots).
4. Set environment variables (API keys the *product* needs — never factory agent keys) in the service's Variables tab.
5. Note the staging URL; it goes in the product's `factory/` notes and every Narrator bundle.

## Conventions

- `main` branch → staging environment. PRs → ephemeral preview environments.
- Postgres backups: Railway's daily backups on; restore procedure noted in the operator runbook. Staging data is disposable by policy — treat any needed data as re-seedable via fixtures.
- Migrations run on deploy via the app's start command (e.g. `npm run migrate && npm start`); the serialized-migrations CI check prevents two tickets carrying conflicting migrations.

This file gets extended during each product's shakedown with whatever reality required (exact start commands, health checks, quirks) — it is deliberately not exhaustive up front.
