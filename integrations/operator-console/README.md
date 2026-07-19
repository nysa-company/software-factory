# Local operator console

Start the zero-dependency console with the installed trust-root launcher and
the factory profile's project registry:

```bash
python3 scripts/operator-console.py
```

The process binds an ephemeral port on `127.0.0.1` and prints one bootstrap
URL. Open that URL once; it is exchanged for an in-memory session carried in
an `HttpOnly; SameSite=Strict` cookie. Restarting the process invalidates both
the bootstrap URL and session. The server rejects non-loopback binds, foreign
Host/Origin values, unlisted routes, missing CSRF tokens, arbitrary paths, and
browser-provided argument vectors.

For an explicit profile or launcher installation:

```bash
python3 scripts/operator-console.py \
  --registry-dir "$HOME/.hermes/profiles/factory/projects" \
  --launcher "$HOME/.factory/bin/factory-launch"
```

Registry `*.env` files are parsed as data, never sourced. Their filename is the
only project selector sent by the browser; `PRODUCT_ROOT` and optional
`KIT_DIR` are validated but never exposed. Each request revalidates the
selected registry file and invokes a fresh fixed launcher command, so one
project cannot provide paths or cached output for another.

## Launcher integration

Contract 1.5 provides the following fixed read-only forms:

```text
factory-launch <project> models policy-candidates --json
factory-launch <project> operator-snapshot workflow --json
factory-launch <project> operator-snapshot envelope --json
factory-launch <project> operator-snapshot spend --json
```

The UI exposes CSRF-protected, preview-hashed controls for project model
policy, envelope limits, temporary envelope overrides, and targeted attempt
cancellation. It also retains model activate/disable/enable endpoints. Every
request maps to an exact launcher grammar; browser-provided paths and arbitrary
argument vectors are never accepted.
