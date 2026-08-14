#!/usr/bin/env python3
"""Focused integration checks for the isolated local release canary."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "scripts/local-release-canary.py"

KIT = r'''#!/usr/bin/env bash
set -euo pipefail
sha=""
product=""
for ((i=1; i<=$#; i++)); do
  value="${!i}"
  if [[ "$value" == "--sha" ]]; then j=$((i+1)); sha="${!j}"; fi
  if [[ "$value" == "--product" ]]; then j=$((i+1)); product="${!j}"; fi
done
if [[ "$1" == "release" && "$2" == "setup" ]]; then
  if [[ -f "$product/factory/SLOW" ]]; then
    sleep 20 &
    printf '%s\n' "$!" > "$FACTORY_RELEASE_TEST_HOME/sleeper.pid"
    wait
  fi
  release="$FACTORY_KITS_ROOT/releases/$sha/scripts"
  mkdir -p "$release" "$HOME/.factory/bin"
  cp "$0" "$release/factory-kit.sh"
  printf '%s\n' "$product" > "$FACTORY_KITS_ROOT/fake-product"
  cat > "$HOME/.factory/bin/factory-launch" <<'PY'
#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys, time
kits = pathlib.Path(os.environ["FACTORY_KITS_ROOT"])
if os.environ.get("FACTORY_LAUNCH_TEST_MODE") != "1" or os.environ.get("FACTORY_LAUNCH_TEST_HOME") != os.environ.get("HOME"):
    raise SystemExit("missing repository-test launcher authority")
project = sys.argv[1]
ticket = "T-1"
sha = next((kits / "releases").iterdir()).name
product = pathlib.Path((kits / "fake-product").read_text().strip())
events = kits / "projects" / project / "controller/events"
events.mkdir(parents=True, exist_ok=True)
for index, name in enumerate(("repository_test_planning", "repository_test_planner_completed")):
    value = {"event": name, "factory_sha": sha, "observed_at_epoch_ns": time.time_ns() + index,
             "schema": "nysa.software-factory.controller-event/v1", "ticket": ticket}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    value["event_sha256"] = hashlib.sha256(raw).hexdigest()
    if index == 0 and (product / "factory/BAD_EVENT").exists():
        value["event_sha256"] = "0" * 64
    path = events / f"{index}.json"
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o600)
print(json.dumps({"status": "planner-complete", "ticket": ticket}, separators=(",", ":")))
PY
  chmod 700 "$HOME/.factory/bin/factory-launch"
  printf '{"schema":"nysa.software-factory.release-plan/v1","stage":"activation","status":"authorized"}\n'
elif [[ "$1" == "release" && "$2" == "resume" ]]; then
  printf '{"factory_sha":"%s","project":"canary","schema":"nysa.software-factory.release-result/v1","status":"pass"}\n' "$sha"
elif [[ "$1" == "operator" && "$2" == "ready" ]]; then
  exit 0
else
  exit 2
fi
'''


def run(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def commit(repo: Path, message: str) -> str:
    run(["/usr/bin/git", "add", "-A"], repo)
    run([
        "/usr/bin/git", "-c", "user.name=Canary", "-c",
        "user.email=canary@example.invalid", "commit", "-m", message,
    ], repo)
    return run(["/usr/bin/git", "rev-parse", "HEAD"], repo)


class LocalReleaseCanaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="local-canary-test.", dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.factory = self.base / "factory-source"
        self.product = self.base / "product-source"
        for repo in (self.factory, self.product):
            repo.mkdir(mode=0o700)
            run(["/usr/bin/git", "init", "-b", "main"], repo)
        (self.factory / "scripts").mkdir()
        (self.factory / "ci/fixtures").mkdir(parents=True)
        kit = self.factory / "scripts/factory-kit.sh"
        kit.write_text(KIT, encoding="utf-8")
        kit.chmod(0o700)
        gh = self.factory / "ci/fixtures/gh-protected-checks"
        gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gh.chmod(0o700)
        self.sha = commit(self.factory, "fixture kit")
        (self.product / "factory/tickets").mkdir(parents=True)
        (self.product / "factory/KIT_PIN").write_text(self.sha + "\n", encoding="utf-8")
        (self.product / "factory/tickets/T-1.md").write_text(
            "Ticket: T-1\nState: Ready\n", encoding="utf-8",
        )
        commit(self.product, "fixture product")
        self.runtime = self.base / "runtime/bin"
        self.runtime.mkdir(parents=True)
        for name in ("node", "npm", "npx"):
            path = self.runtime / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o700)
        self.tools = []
        for name in ("gitleaks", "claude", "codex", "cursor"):
            path = self.base / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o700)
            self.tools.append(path)

    def command(self, root: Path, maximum: int = 30) -> list[str]:
        return [
            "python3", str(CANARY), "--factory", str(self.factory),
            "--product", str(self.product), "--project", "canary",
            "--ticket", "T-1", "--profile", "test-profile",
            "--operator-id", "tester", "--runtime-bin", str(self.runtime),
            "--gitleaks-bin", str(self.tools[0]), "--claude-bin", str(self.tools[1]),
            "--codex-bin", str(self.tools[2]), "--cursor-bin", str(self.tools[3]),
            "--root", str(root), "--max-seconds", str(maximum),
        ]

    def invoke(self, root: Path, maximum: int = 30) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = subprocess.run(
            self.command(root, maximum), text=True, capture_output=True, check=False,
        )
        return result, json.loads(result.stdout)

    def test_runs_setup_activation_and_one_authenticated_planner(self) -> None:
        result, value = self.invoke(self.base / "canary")
        self.assertEqual(result.returncode, 0, result.stderr or value)
        self.assertEqual(value["status"], "pass")
        self.assertFalse(value["production_evidence"])
        self.assertEqual(value["trust_scope"], "repository-test")
        self.assertEqual(value["ticket"], "T-1")
        self.assertLess(value["elapsed_seconds"], 30)
        self.assertEqual(
            [item["name"] for item in value["commands"]],
            ["release-setup", "release-resume-1", "controller-reconcile"],
        )
        self.assertEqual(set(value["events"]), {"planning", "planner_completed"})
        self.assertTrue(Path(value["root"]).is_relative_to(self.base))

    def test_refuses_dirty_source_before_creating_root(self) -> None:
        (self.product / "dirty").write_text("dirty\n", encoding="utf-8")
        root = self.base / "unused"
        result, value = self.invoke(root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be clean", value["error"])
        self.assertFalse(root.exists())

    def test_refuses_bad_controller_event(self) -> None:
        (self.product / "factory/BAD_EVENT").write_text("1\n", encoding="utf-8")
        commit(self.product, "bad event fixture")
        result, value = self.invoke(self.base / "bad-event")
        self.assertEqual(result.returncode, 2)
        self.assertIn("evidence is invalid", value["error"])

    def test_refuses_unavailable_product_pin_before_creating_root(self) -> None:
        (self.product / "factory/KIT_PIN").write_text("1" * 40 + "\n", encoding="utf-8")
        commit(self.product, "unavailable pin fixture")
        root = self.base / "unused-pin"
        result, value = self.invoke(root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not contain the pinned release", value["error"])
        self.assertFalse(root.exists())

    def test_refuses_ticket_that_is_not_ready(self) -> None:
        (self.product / "factory/tickets/T-1.md").write_text(
            "Ticket: T-1\nState: Backlog\n", encoding="utf-8",
        )
        commit(self.product, "backlog ticket fixture")
        root = self.base / "unused-ticket"
        result, value = self.invoke(root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("must have exactly one Ready state", value["error"])
        self.assertFalse(root.exists())

    def test_refuses_nonempty_evidence_root(self) -> None:
        root = self.base / "occupied"
        root.mkdir(mode=0o700)
        (root / "keep").write_text("preserve\n", encoding="utf-8")
        result, value = self.invoke(root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be empty", value["error"])
        self.assertEqual((root / "keep").read_text(), "preserve\n")

    def test_timeout_terminates_the_command_process_group(self) -> None:
        (self.product / "factory/SLOW").write_text("1\n", encoding="utf-8")
        commit(self.product, "slow fixture")
        root = self.base / "timeout"
        result, value = self.invoke(root, maximum=1)
        self.assertEqual(result.returncode, 2)
        self.assertIn("exceeded the canary time limit", value["error"])
        pid = int((root / "sleeper.pid").read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("timed-out canary left its child process running")


if __name__ == "__main__":
    unittest.main()
