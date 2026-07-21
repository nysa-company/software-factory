# Contract 1.6 local four-worker evidence — 2026-07-20

- Host: Apple arm64, 8 logical CPUs, 8 GiB RAM.
- Colima: macOS Virtualization.Framework, Docker runtime, 4 CPUs, 4 GiB RAM,
  100 GiB disk.
- Preserved services: `nysa-factory-cert-postgres`, `nysa-test-pg`, and
  `gbrain-test-pg` restarted after the resize and each passed `pg_isready`.
- Worker limits: 0.5 CPU, 512 MiB, 128 PIDs, read-only root, all capabilities
  dropped, no-new-privileges, network disabled, attempt-local tmpfs.
- Image:
  `node:22-bookworm@sha256:5647be709086c696ff32edaaf1c70cd26d1da6ab2b39c32f3c7b4c4a31957e37`.

Two saturated sandbox waves passed at every staged capacity:

| Capacity | Wave 1 | Wave 2 | Refusal |
|---:|---:|---:|---|
| 1 | 8.69 s | 8.75 s | attempt 2 |
| 2 | 8.53 s | 8.90 s | attempt 3 |
| 4 | 11.13 s | 10.67 s | attempt 5 |

All admitted attempts had unique container identities, completed concurrently,
terminalized transactionally, and were removed. Every over-capacity attempt was
denied before container creation. During the staged run, host memory reported
31% free; the three PostgreSQL containers remained available.

Real-provider promotion remains disabled because the fixed owner-local broker
configuration, activation record, TLS trust anchor, and dedicated API
credentials are not present under `~/.factory`. Native subscription and Cursor
CLI routes remain on the serialized legacy path. This missing external
credential prerequisite does not weaken the sandbox certification and must not
be bypassed by copying host login state into a worker.
