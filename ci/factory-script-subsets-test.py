#!/usr/bin/env python3
"""Focused regressions for factory-script subset orchestration."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "ci" / "factory-script-subsets.py"


def run(
    stub_body: str,
) -> tuple[subprocess.CompletedProcess[str], Path, tempfile.TemporaryDirectory[str]]:
    temporary = tempfile.TemporaryDirectory(prefix="factory-subsets-test-")
    root = Path(temporary.name)
    stub = root / "worker.sh"
    stub.write_text("#!/usr/bin/env bash\nset -u\n" + stub_body, encoding="utf-8")
    stub.chmod(0o700)
    environment = os.environ.copy()
    environment["FACTORY_SUBSET_TEST_STATE"] = str(root / "state")
    (root / "state").mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--script",
            str(stub),
            "--temp-root",
            str(root / "workers"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result, root, temporary


success, success_root, success_temporary = run(
    r'''
subset="$2"
touch "$FACTORY_SUBSET_TEST_STATE/started-$subset"
for _ in $(seq 1 500); do
  set -- "$FACTORY_SUBSET_TEST_STATE"/started-*
  [[ "$#" -eq 6 ]] && break
  sleep 0.01
done
[[ "$#" -eq 6 ]]
printf '%s\n' "$subset" > "$TMPDIR/fixture"
echo "PASS: $subset probe"
'''
)
assert success.returncode == 0, success.stdout + success.stderr
fixtures = sorted((success_root / "workers").glob("*/fixture"))
assert len(fixtures) == 6
assert {path.read_text(encoding="utf-8").strip() for path in fixtures} == {
    "model-policy",
    "runtime-routing",
    "launch-controls",
    "sequencer",
    "role-exit-git",
    "role-exit-policy",
}
success_temporary.cleanup()

failure, _failure_root, failure_temporary = run(
    r'''
subset="$2"
if [[ "$subset" == runtime-routing ]]; then
  echo "FAIL: exact runtime assertion"
  exit 7
fi
sleep 30
'''
)
assert failure.returncode == 1
assert "FAIL: exact runtime assertion" in failure.stdout, failure.stdout + failure.stderr
assert "factory-script subset runtime-routing exited 7" in failure.stdout, failure.stdout
failure_temporary.cleanup()

leak, leak_root, leak_temporary = run(
    r'''
subset="$2"
if [[ "$subset" == role-exit-policy ]]; then
  sleep 30 &
  echo "$!" > "$FACTORY_SUBSET_TEST_STATE/leaked.pid"
fi
'''
)
assert leak.returncode == 1
assert "subset leaked a child process" in leak.stdout
leaked_pid = int((leak_root / "state" / "leaked.pid").read_text(encoding="utf-8"))
for _ in range(100):
    try:
        os.kill(leaked_pid, 0)
    except ProcessLookupError:
        break
    time.sleep(0.01)
else:
    raise AssertionError("orchestration left the leaked child alive")
leak_temporary.cleanup()

print("PASS: factory-script subset orchestration isolates roots and cleans children")
