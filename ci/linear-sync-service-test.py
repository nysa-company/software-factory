#!/usr/bin/env python3
"""Disposable transactional tests for Linear sync LaunchAgent migration."""

import fcntl
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/linear-sync-service.py"


FAKE_LAUNCHCTL = r'''#!/usr/bin/env python3
import json, os, pathlib, plistlib, sys
state_path = pathlib.Path(os.environ["FAKE_LAUNCHCTL_STATE"])
trace_path = pathlib.Path(os.environ["FAKE_LAUNCHCTL_TRACE"])
state = json.loads(state_path.read_text())
command, *arguments = sys.argv[1:]
with trace_path.open("a") as stream:
    stream.write(command + "\n")
if command == "print-disabled":
    if state.pop("fail_next_print_disabled", False):
        state_path.write_text(json.dumps(state))
        print("injected print-disabled failure", file=sys.stderr)
        raise SystemExit(5)
    print('disabled services = {')
    if state["state"] != "unspecified":
        print(f'  "com.factory.linear-sync.alpha" => {state["state"]}')
    if state.get("duplicate_state"):
        print('  "com.factory.linear-sync.alpha" => disabled')
    print('}')
elif command == "print":
    if state.get("fail_print") or state.pop("fail_next_print", False):
        state_path.write_text(json.dumps(state))
        print("injected print failure", file=sys.stderr)
        raise SystemExit(5)
    if not state["loaded"]:
        raise SystemExit(113)
    print(arguments[0] + " = {")
    print("  arguments = {")
    for item in state["arguments"]:
        print("    " + item)
    print("  }")
    print("}")
elif command == "bootout":
    if not state["loaded"]:
        raise SystemExit(113)
    state["loaded"] = False
elif command in ("enable", "disable"):
    count_key = "_" + command + "_count"
    failure_key = "fail_" + command + "_at"
    if failure_key in state or (
        command == "enable" and "indeterminate_after_enable_at" in state
    ):
        state[count_key] = state.get(count_key, 0) + 1
    if failure_key in state and state[failure_key] == state.get(count_key):
        state_path.write_text(json.dumps(state))
        print("injected " + command + " failure", file=sys.stderr)
        raise SystemExit(5)
    state["state"] = command + "d"
    if (
        "indeterminate_after_enable_at" in state
        and state["indeterminate_after_enable_at"] == state.get("_enable_count")
    ):
        state["fail_next_print_disabled"] = True
elif command == "bootstrap":
    if state.get("fail_bootstrap"):
        state["fail_bootstrap"] = False
        state_path.write_text(json.dumps(state))
        print("injected bootstrap failure", file=sys.stderr)
        raise SystemExit(5)
    with open(arguments[1], "rb") as stream:
        state["arguments"] = plistlib.load(stream)["ProgramArguments"]
    state["loaded"] = True
    if state.pop("fail_query_after_bootstrap", False):
        state["fail_next_print"] = True
else:
    raise SystemExit(2)
state_path.write_text(json.dumps(state))
'''


def render_old_plist(path: Path, product: Path) -> bytes:
    value = {
        "Label": "com.factory.linear-sync.alpha",
        "ProgramArguments": [
            "/usr/bin/env", "python3", "/old/release/scripts/linear-sync.py",
            "--factory-root", str(product),
        ],
        "StartInterval": 180,
        "RunAtLoad": True,
        "StandardOutPath": str(product / "factory/linear-sync.log"),
        "StandardErrorPath": str(product / "factory/linear-sync.err.log"),
    }
    content = plistlib.dumps(value)
    path.write_bytes(content)
    return content


def invoke(home: Path, product: Path, release: Path, launchctl: Path, action: str):
    environment = {
        **os.environ,
        "HOME": str(home),
        "FACTORY_KIT_TEST_MODE": "1",
        "FACTORY_KIT_TEST_LAUNCHCTL": str(launchctl),
        "FAKE_LAUNCHCTL_STATE": str(home / "launchctl-state.json"),
        "FAKE_LAUNCHCTL_TRACE": str(home / "launchctl-trace.txt"),
    }
    return subprocess.run(
        [
            str(HELPER), action, "--project", "alpha", "--product", str(product),
            "--release", str(release), "--launcher",
            str(home / ".factory/bin/factory-launch"),
        ],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


with tempfile.TemporaryDirectory(prefix="linear-sync-service-test.") as temporary:
    root = Path(temporary)
    home = root / "home"
    product = root / "product"
    release = root / "release"
    launcher = home / ".factory/bin/factory-launch"
    release_launcher = release / "integrations/hermes/bin/factory-launch"
    destination = home / "Library/LaunchAgents/com.factory.linear-sync.alpha.plist"
    launchctl = root / "launchctl"
    for directory in (
        product / "factory", release / "scripts/launchd", release_launcher.parent,
        launcher.parent,
        destination.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(
        (ROOT / "integrations/hermes/bin/factory-launch").read_bytes()
    )
    launcher.chmod(0o700)
    release_launcher.write_bytes(launcher.read_bytes())
    release_launcher.chmod(0o700)
    (release / "scripts/launchd/com.factory.linear-sync.plist.template").write_bytes(
        (ROOT / "scripts/launchd/com.factory.linear-sync.plist.template").read_bytes()
    )
    launchctl.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
    launchctl.chmod(0o700)
    state_path = home / "launchctl-state.json"
    trace_path = home / "launchctl-trace.txt"
    trace_path.write_text("")

    old = render_old_plist(destination, product)
    legacy_arguments = plistlib.loads(old)["ProgramArguments"]
    state_path.write_text(json.dumps({
        "state": "enabled", "loaded": True, "arguments": legacy_arguments,
    }))
    cycle_held = threading.Event()
    try_map = threading.Event()
    map_acquired = threading.Event()

    def active_cycle():
        with (product / "factory/.linear-sync-cycle.lock").open("a") as cycle_lock:
            with (product / "factory/.linear-sync.lock").open("a") as map_lock:
                fcntl.flock(cycle_lock, fcntl.LOCK_EX)
                cycle_held.set()
                assert try_map.wait(5), "lock-order test did not release worker"
                fcntl.flock(map_lock, fcntl.LOCK_EX)
                map_acquired.set()
                fcntl.flock(map_lock, fcntl.LOCK_UN)
                fcntl.flock(cycle_lock, fcntl.LOCK_UN)

    worker = threading.Thread(target=active_cycle, daemon=True)
    worker.start()
    assert cycle_held.wait(5), "active cycle did not acquire the cycle lock"
    outcome = {}
    migration = threading.Thread(
        target=lambda: outcome.setdefault(
            "result", invoke(home, product, release, launchctl, "enable")
        ),
        daemon=True,
    )
    migration.start()
    time.sleep(0.1)
    assert migration.is_alive(), "service migration did not drain the active cycle"
    try_map.set()
    assert map_acquired.wait(5), "service migration acquired the map lock out of order"
    worker.join(timeout=5)
    assert not worker.is_alive(), "active cycle did not finish"
    migration.join(timeout=10)
    assert not migration.is_alive(), "service migration did not resume after cycle drain"
    result = outcome["result"]
    assert result.returncode == 0, result.stderr
    assert "state=enabled" in result.stdout
    assert "OWNERSHIP ADOPTED" not in result.stdout
    migrated = plistlib.loads(destination.read_bytes())
    expected = [str(launcher.resolve()), "alpha", "linear-sync"]
    assert migrated["ProgramArguments"] == expected
    state = json.loads(state_path.read_text())
    assert state == {"state": "enabled", "loaded": True, "arguments": expected}

    state_path.write_text(json.dumps({
        "state": "false", "loaded": True, "arguments": expected,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode == 0, result.stderr
    assert json.loads(state_path.read_text()) == {
        "state": "enabled", "loaded": True, "arguments": expected,
    }

    result = invoke(home, product, release, launchctl, "disable")
    assert result.returncode == 0, result.stderr
    assert "state=disabled" in result.stdout
    state = json.loads(state_path.read_text())
    assert state == {"state": "disabled", "loaded": False, "arguments": expected}

    destination.unlink()
    trace_path.write_text("")
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": False, "arguments": legacy_arguments,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode == 0, result.stderr
    assert "LINEAR SYNC SERVICE OWNERSHIP ADOPTED" in result.stdout
    assert plistlib.loads(destination.read_bytes())["ProgramArguments"] == expected
    assert json.loads(state_path.read_text()) == {
        "state": "enabled", "loaded": True, "arguments": expected,
    }

    destination.unlink()
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": False, "arguments": legacy_arguments,
    }))
    result = invoke(home, product, release, launchctl, "disable")
    assert result.returncode == 0, result.stderr
    assert "LINEAR SYNC SERVICE OWNERSHIP ADOPTED" in result.stdout
    assert json.loads(state_path.read_text()) == {
        "state": "disabled", "loaded": False, "arguments": legacy_arguments,
    }

    destination.unlink()
    trace_path.write_text("")
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": True, "arguments": legacy_arguments,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode != 0
    assert "no restorable plist" in result.stderr
    assert "OWNERSHIP ADOPTED" not in result.stdout
    assert trace_path.read_text().splitlines() == ["print-disabled", "print"]
    assert not destination.exists()

    old = render_old_plist(destination, product)
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": True, "arguments": legacy_arguments,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode == 0, result.stderr
    assert "LINEAR SYNC SERVICE OWNERSHIP ADOPTED" in result.stdout
    assert json.loads(state_path.read_text()) == {
        "state": "enabled", "loaded": True, "arguments": expected,
    }

    old = render_old_plist(destination, product)
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": True, "arguments": legacy_arguments,
    }))
    result = invoke(home, product, release, launchctl, "disable")
    assert result.returncode == 0, result.stderr
    assert "LINEAR SYNC SERVICE OWNERSHIP ADOPTED" in result.stdout
    assert json.loads(state_path.read_text()) == {
        "state": "disabled", "loaded": False, "arguments": legacy_arguments,
    }

    old = render_old_plist(destination, product)
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": False, "arguments": legacy_arguments,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode == 0, result.stderr
    assert "LINEAR SYNC SERVICE OWNERSHIP ADOPTED" in result.stdout
    assert json.loads(state_path.read_text()) == {
        "state": "enabled", "loaded": True, "arguments": expected,
    }

    old = render_old_plist(destination, product)
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": False, "arguments": legacy_arguments,
    }))
    result = invoke(home, product, release, launchctl, "disable")
    assert result.returncode == 0, result.stderr
    assert "LINEAR SYNC SERVICE OWNERSHIP ADOPTED" in result.stdout
    assert json.loads(state_path.read_text()) == {
        "state": "disabled", "loaded": False, "arguments": legacy_arguments,
    }

    old = render_old_plist(destination, product)
    trace_path.write_text("")
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": False, "arguments": legacy_arguments,
        "fail_enable_at": 1,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode != 0
    assert "ownership adoption failed or is indeterminate" in result.stderr
    assert "service migration was not attempted" in result.stderr
    assert "rolled back" not in result.stderr
    assert "OWNERSHIP ADOPTED" not in result.stdout
    assert trace_path.read_text().splitlines() == ["print-disabled", "print", "enable"]
    assert destination.read_bytes() == old

    old = render_old_plist(destination, product)
    trace_path.write_text("")
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": False, "arguments": legacy_arguments,
        "indeterminate_after_enable_at": 1,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode != 0
    assert "ownership adoption failed or is indeterminate" in result.stderr
    assert "service migration was not attempted" in result.stderr
    assert "rolled back" not in result.stderr
    assert trace_path.read_text().splitlines() == [
        "print-disabled", "print", "enable", "print-disabled",
    ]
    assert destination.read_bytes() == old
    assert json.loads(state_path.read_text())["state"] == "enabled"

    old = render_old_plist(destination, product)
    old_mode = destination.stat().st_mode & 0o777
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": False, "arguments": legacy_arguments,
        "fail_enable_at": 2,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode != 0
    assert "LINEAR SYNC SERVICE OWNERSHIP ADOPTED" in result.stdout
    assert "rolled back" in result.stderr
    assert destination.read_bytes() == old
    assert destination.stat().st_mode & 0o777 == old_mode
    state = json.loads(state_path.read_text())
    assert state["state"] == "enabled"
    assert state["loaded"] is False

    old = render_old_plist(destination, product)
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": True, "arguments": legacy_arguments,
        "fail_bootstrap": True,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode != 0
    assert "LINEAR SYNC SERVICE OWNERSHIP ADOPTED" in result.stdout
    assert "rolled back" in result.stderr
    assert destination.read_bytes() == old
    state = json.loads(state_path.read_text())
    assert state["state"] == "enabled"
    assert state["loaded"] is True
    assert state["arguments"] == legacy_arguments

    old = render_old_plist(destination, product)
    state_path.write_text(json.dumps({
        "state": "unspecified", "loaded": True, "arguments": legacy_arguments,
        "fail_query_after_bootstrap": True,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode != 0
    assert "LINEAR SYNC SERVICE OWNERSHIP ADOPTED" in result.stdout
    assert "rolled back" in result.stderr
    assert destination.read_bytes() == old
    state = json.loads(state_path.read_text())
    assert state["state"] == "enabled"
    assert state["loaded"] is True
    assert state["arguments"] == legacy_arguments

    old = render_old_plist(destination, product)
    old_mode = destination.stat().st_mode & 0o777
    state_path.write_text(json.dumps({
        "state": "enabled", "loaded": True, "arguments": legacy_arguments,
        "fail_bootstrap": True,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode != 0
    assert "rolled back" in result.stderr
    assert destination.read_bytes() == old
    assert destination.stat().st_mode & 0o777 == old_mode
    state = json.loads(state_path.read_text())
    assert state["state"] == "enabled"
    assert state["loaded"] is True
    assert state["arguments"] == legacy_arguments

    launcher.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode != 0
    assert "installed launcher does not match" in result.stderr
    assert destination.read_bytes() == old
    launcher.write_bytes(release_launcher.read_bytes())
    launcher.chmod(0o700)

    state_path.write_text(json.dumps({
        "state": "enabled", "loaded": True, "arguments": legacy_arguments,
        "duplicate_state": True,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode != 0
    assert "disabled state is unreadable" in result.stderr
    assert destination.read_bytes() == old
    assert json.loads(state_path.read_text()) == {
        "state": "enabled", "loaded": True, "arguments": legacy_arguments,
        "duplicate_state": True,
    }

    state_path.write_text(json.dumps({
        "state": "enabled", "loaded": True, "arguments": legacy_arguments,
        "fail_print": True,
    }))
    result = invoke(home, product, release, launchctl, "enable")
    assert result.returncode != 0
    assert "launchctl print failed" in result.stderr
    assert destination.read_bytes() == old
    assert json.loads(state_path.read_text()) == {
        "state": "enabled", "loaded": True, "arguments": legacy_arguments,
        "fail_print": True,
    }

print("linear-sync-service-test: all cases passed")
