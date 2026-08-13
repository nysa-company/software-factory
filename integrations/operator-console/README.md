# Local operator console

Start the zero-dependency console with the installed trust-root launcher and
the Factory's active project records:

```bash
python3 scripts/operator-console.py
```

The process binds an ephemeral port on `127.0.0.1` and prints one bootstrap
URL. Open that URL once; it is exchanged for an in-memory session carried in
an `HttpOnly; SameSite=Strict` cookie. Restarting the process invalidates both
the bootstrap URL and session. The server rejects non-loopback binds, foreign
Host/Origin values, unlisted routes, missing CSRF tokens, arbitrary paths, and
browser-provided argument vectors.

For an explicit Factory state or launcher installation:

```bash
python3 scripts/operator-console.py \
  --projects-dir "$HOME/.factory/kits/projects" \
  --launcher "$HOME/.factory/bin/factory-launch"
```

Each `<slug>/active.json` is used only to discover an active project slug. The
record must be an owner-controlled, mode-0600 physical file and its `project`
field must match the directory. Product and release paths remain the installed
launcher's authority and are never read or exposed by the console. Each request
revalidates the active records and invokes a fresh fixed launcher command, so
one project cannot provide paths or cached output for another.

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
