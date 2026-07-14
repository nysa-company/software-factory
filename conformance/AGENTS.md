# Relay conformance scope

This subtree proves the reusable factory against a synthetic Node.js app. Root `AGENTS.md` still applies; these are only the local differences.

## Commands

- App tests: `npm --prefix conformance/app test`
- Local app: `PORT="${CONDUCTOR_PORT:-4700}" npm --prefix conformance/app start`
- Full factory verification still runs from the repository root: `bash ci/test-all.sh`.

## Boundaries

- Keep fixtures synthetic. Never add customer, financial, credential, or production-derived data.
- Preserve test immutability: test-author commits precede implementation commits, and implementation commits do not edit `conformance/app/tests/`.
- Treat changes under `conformance/factory/` as product-instance evidence, not generic engine behavior. Generic behavior belongs in the root kit and must keep `FACTORY.md` synchronized.
- Runtime state belongs under `conformance/app/data/` and stays untracked.
