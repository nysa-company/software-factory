#!/usr/bin/env python3
"""Atomically migrate one product's scheduled Linear sync LaunchAgent."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import tempfile
from xml.sax.saxutils import escape


def fail(message: str) -> None:
    raise SystemExit(message)


def run(
    launchctl: str, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [launchctl, *arguments], text=True, capture_output=True, check=False
    )
    if check and result.returncode:
        raise RuntimeError(f"launchctl {arguments[0]} failed")
    return result


def disabled_state(launchctl: str, domain: str, label: str) -> str:
    result = run(launchctl, "print-disabled", domain)
    states: dict[str, str] = {}
    opened = False
    closed = False
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        if not opened:
            if not re.fullmatch(r"\s*disabled services\s*=\s*\{\s*", line):
                raise RuntimeError("launchctl disabled state is unreadable")
            opened = True
            continue
        if re.fullmatch(r"\s*\}\s*", line):
            if closed:
                raise RuntimeError("launchctl disabled state is unreadable")
            closed = True
            continue
        if closed:
            raise RuntimeError("launchctl disabled state is unreadable")
        match = re.fullmatch(
            r'\s*"([^"\r\n]+)"\s*=>\s*(enabled|disabled|true|false)\s*',
            line,
        )
        if match is None or match.group(1) in states:
            raise RuntimeError("launchctl disabled state is unreadable")
        states[match.group(1)] = match.group(2)
    if not opened or not closed:
        raise RuntimeError("launchctl disabled state is unreadable")
    state = states.get(label)
    if state in {"enabled", "false"}:
        return "enabled"
    if state in {"disabled", "true"}:
        return "disabled"
    return "unspecified"


def loaded_arguments(launchctl: str, target: str) -> list[str] | None:
    result = run(launchctl, "print", target, check=False)
    if result.returncode == 113:
        return None
    if result.returncode:
        raise RuntimeError("launchctl print failed")
    matches = re.findall(
        r"^\s*arguments = \{\n(?P<body>.*?)^\s*\}\s*$",
        result.stdout,
        re.MULTILINE | re.DOTALL,
    )
    if len(matches) != 1:
        raise RuntimeError("loaded service arguments are unreadable")
    return [line.strip() for line in matches[0].splitlines() if line.strip()]


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    parent = path.parent.lstat()
    if (
        path.parent.is_symlink()
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o022
    ):
        raise RuntimeError("LaunchAgents directory is unsafe")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def validate_owned_regular(path: Path, *, executable: bool = False) -> None:
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or (executable and not os.access(path, os.X_OK))
    ):
        raise RuntimeError(f"unsafe file: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("enable", "disable"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--launcher", required=True)
    arguments = parser.parse_args()

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", arguments.project):
        fail("invalid project slug")
    product = Path(arguments.product).resolve(strict=True)
    release = Path(arguments.release).resolve(strict=True)
    launcher = Path(arguments.launcher).resolve(strict=True)
    home = Path.home().resolve(strict=True)
    if launcher != home / ".factory/bin/factory-launch":
        fail("installed launcher path is not canonical")
    template = release / "scripts/launchd/com.factory.linear-sync.plist.template"
    release_launcher = release / "integrations/hermes/bin/factory-launch"
    validate_owned_regular(template)
    validate_owned_regular(release_launcher, executable=True)
    validate_owned_regular(launcher, executable=True)
    if launcher.read_bytes() != release_launcher.read_bytes():
        fail("installed launcher does not match the active release")

    launchctl = "/bin/launchctl"
    override = os.environ.get("FACTORY_KIT_TEST_LAUNCHCTL")
    if override:
        if os.environ.get("FACTORY_KIT_TEST_MODE") != "1":
            fail("launchctl override is test-only")
        launchctl = str(Path(override).resolve(strict=True))
    if not Path(launchctl).is_file() or not os.access(launchctl, os.X_OK):
        fail("launchctl is unavailable")

    label = f"com.factory.linear-sync.{arguments.project}"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    destination = home / f"Library/LaunchAgents/{label}.plist"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (home / "Library").is_symlink() or destination.parent.is_symlink():
        fail("LaunchAgents path contains a symlink")
    if destination.exists() or destination.is_symlink():
        validate_owned_regular(destination)

    # Current releases hold the cycle lock for the whole reconciliation. Older
    # installed releases used the map lock for that purpose. Acquire in this
    # order so a current cycle can finish its nested map write without deadlock.
    sync_locks: list[int] = []
    for name in (".linear-sync-cycle.lock", ".linear-sync.lock"):
        lock_path = product / "factory" / name
        if lock_path.is_symlink():
            fail("Linear sync lock is unsafe")
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            fail("Linear sync lock is unsafe")
        sync_locks.append(descriptor)
    for descriptor in sync_locks:
        fcntl.flock(descriptor, fcntl.LOCK_EX)

    rendered = (
        template.read_text(encoding="utf-8")
        .replace("__HOME__", escape(str(home)))
        .replace("__PROJECT_SLUG__", arguments.project)
        .replace("__FACTORY_ROOT__", escape(str(product)))
    )
    if re.search(r"__[A-Z0-9_]+__", rendered):
        fail("Linear sync LaunchAgent contains an unresolved placeholder")
    document = plistlib.loads(rendered.encode())
    expected_arguments = [str(launcher), arguments.project, "linear-sync"]
    if (
        document.get("Label") != label
        or document.get("ProgramArguments") != expected_arguments
        or document.get("StartInterval") != 180
        or document.get("RunAtLoad") is not True
        or document.get("StandardOutPath") != str(product / "factory/linear-sync.log")
        or document.get("StandardErrorPath")
        != str(product / "factory/linear-sync.err.log")
        or set(document) != {
            "Label", "ProgramArguments", "StartInterval", "RunAtLoad",
            "StandardOutPath", "StandardErrorPath",
        }
    ):
        fail("Linear sync LaunchAgent template is invalid")
    rendered_bytes = plistlib.dumps(document, sort_keys=False)

    previous_bytes = destination.read_bytes() if destination.exists() else None
    previous_mode = (
        stat.S_IMODE(destination.stat().st_mode) if destination.exists() else 0o600
    )
    previous_state = disabled_state(launchctl, domain, label)
    previous_arguments = loaded_arguments(launchctl, target)
    previous_loaded = previous_arguments is not None
    if previous_loaded and previous_bytes is None:
        fail("loaded Linear sync service has no restorable plist")
    if previous_state == "unspecified":
        try:
            run(launchctl, "enable", target)
            if disabled_state(launchctl, domain, label) != "enabled":
                raise RuntimeError("explicit enabled state was not verified")
        except Exception as error:
            fail(
                "Linear sync service ownership adoption failed or is "
                f"indeterminate; service migration was not attempted: {error}"
            )
        previous_state = "enabled"
        print(
            f"LINEAR SYNC SERVICE OWNERSHIP ADOPTED: project={arguments.project} "
            f"state=enabled target={target}"
        )
    mutated = False

    def restore() -> None:
        run(launchctl, "bootout", target, check=False)
        if previous_bytes is None:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write(destination, previous_bytes, previous_mode)
        run(launchctl, "enable", target)
        if previous_loaded and previous_bytes is not None:
            run(launchctl, "bootstrap", domain, str(destination))
        if previous_state == "disabled":
            run(launchctl, "disable", target)
        restored_state = disabled_state(launchctl, domain, label)
        if restored_state != previous_state:
            raise RuntimeError("previous enable/disable state was not restored")
        if loaded_arguments(launchctl, target) != previous_arguments:
            raise RuntimeError("previous loaded service arguments were not restored")
        if previous_bytes is None:
            if destination.exists() or destination.is_symlink():
                raise RuntimeError("previous missing plist state was not restored")
        elif (
            destination.read_bytes() != previous_bytes
            or stat.S_IMODE(destination.stat().st_mode) != previous_mode
        ):
            raise RuntimeError("previous plist bytes were not restored")

    try:
        if previous_loaded:
            mutated = True
            run(launchctl, "bootout", target)
        mutated = True
        atomic_write(destination, rendered_bytes)
        run(launchctl, arguments.action, target)
        if arguments.action == "enable":
            run(launchctl, "bootstrap", domain, str(destination))
            if disabled_state(launchctl, domain, label) != "enabled":
                raise RuntimeError("service did not become explicitly enabled")
            if loaded_arguments(launchctl, target) != expected_arguments:
                raise RuntimeError("loaded service does not use the stable launcher")
        else:
            if disabled_state(launchctl, domain, label) != "disabled":
                raise RuntimeError("service did not become explicitly disabled")
            if loaded_arguments(launchctl, target) is not None:
                raise RuntimeError("disabled service remains loaded")
    except Exception as error:
        if mutated:
            try:
                restore()
            except Exception as rollback_error:
                fail(
                    "Linear sync service migration failed "
                    f"({error}); rollback failed ({rollback_error})"
                )
        fail(f"Linear sync service migration failed and was rolled back: {error}")

    print(
        f"LINEAR SYNC SERVICE OK: project={arguments.project} "
        f"state={arguments.action}d target={target}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        fail(f"Linear sync service migration refused: {error}")
