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

## PR-environment variable overrides (root-cause fix for preview drift)

One-time operator action per product, on the Railway PR-environments template: set the web service's API URL and the API service's web-origin URL as Railway *reference variables* instead of inheriting production's fixed custom-domain values — e.g. `VITE_API_URL=https://${{api.RAILWAY_PUBLIC_DOMAIN}}` on the web service, `WEB_APP_URL=https://${{web.RAILWAY_PUBLIC_DOMAIN}}` on the API service (adjust service names to whatever this product's services are called).

Why: T-009's PR preview served a web bundle still pointing at the production API, which invalidated the Narrator evidence pass after review approval. A hardcoded production URL doesn't change per PR environment; a reference variable resolves to that PR environment's own sibling service, so every PR environment self-pairs automatically.

## Non-interactive access (project token)

Mint a least-privilege Railway project token once via the dashboard (Settings → Tokens), scoped to this product's services only. Store it at `~/.hermes/secrets/railway-token`, `chmod 600`, following the same owner-only `~/.hermes/secrets/` convention used for every other Factory-managed credential.

Usage: export it as `RAILWAY_TOKEN` for the `railway` CLI so redeploys and variable changes never wait on an operator browser session. Never commit it, never print it.

This file gets extended during each product's shakedown with whatever reality required (exact start commands, health checks, quirks) — it is deliberately not exhaustive up front.
