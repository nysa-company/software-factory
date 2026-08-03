#!/usr/bin/env python3
"""Install and verify owner-local Node/npm/npx pins for the launcher PATH."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts/lib"))

from certification_plan import safe_plan, validate_plan  # noqa: E402


TOOLS = ("node", "npm", "npx")
SAFE_PATH_SUFFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class PinError(ValueError):
    pass


def secure_directory(path: Path, label: str) -> None:
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or path.resolve(strict=True) != path
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise PinError(f"{label} is unsafe")


def executable(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as error:
        raise PinError(f"{label} is missing") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise PinError(f"{label} is unsafe")
    return resolved


def version(command: str, path: str, home: Path) -> str:
    try:
        result = subprocess.run(
            [command, "--version"],
            check=False,
            capture_output=True,
            env={"HOME": str(home), "PATH": path},
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def expected_runtime(product: Path) -> dict[str, str]:
    plan_path = product / "factory/certification-plan.json"
    try:
        plan, _ = safe_plan(plan_path)
        validate_plan(plan, product)
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise PinError("product certification plan is invalid") from error
    return {**plan["runtime"], "npx": plan["runtime"]["npm"]}


def verify_runtime(
    expected: dict[str, str], commands: dict[str, str], path: str, home: Path
) -> dict[str, str]:
    observed = {tool: version(commands[tool], path, home) for tool in TOOLS}
    for tool in TOOLS:
        if observed[tool] != expected[tool]:
            raise PinError(
                f"runtime mismatch for {tool}: expected {expected[tool]}, "
                f"observed {observed[tool] or 'missing'}"
            )
    return observed


def atomic_link(path: Path, target: Path) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def restore(path: Path, previous: str | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_link(path, Path(previous))


def pin(product: Path, runtime_bin: Path, home: Path) -> dict[str, str]:
    secure_directory(home, "HOME")
    product = product.resolve(strict=True)
    secure_directory(product, "product")
    runtime_bin = runtime_bin.resolve(strict=True)
    secure_directory(runtime_bin, "runtime bin")

    expected = expected_runtime(product)
    sources = {
        tool: executable(runtime_bin / tool, f"runtime {tool}") for tool in TOOLS
    }
    source_path = f"{runtime_bin}:{SAFE_PATH_SUFFIX}"
    verify_runtime(
        expected,
        {tool: str(sources[tool]) for tool in TOOLS},
        source_path,
        home,
    )

    factory = home / ".factory"
    factory.mkdir(mode=0o700, exist_ok=True)
    secure_directory(factory, "owner Factory directory")
    target_bin = factory / "bin"
    target_bin.mkdir(mode=0o700, exist_ok=True)
    secure_directory(target_bin, "owner launcher bin")

    lock_path = factory / ".runtime-pin.lock"
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise PinError("runtime pin lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)

        previous: dict[str, str | None] = {}
        for tool in TOOLS:
            target = target_bin / tool
            if target.is_symlink():
                if target.lstat().st_uid != os.geteuid():
                    raise PinError(f"existing {tool} pin is unsafe")
                previous[tool] = os.readlink(target)
            elif target.exists():
                raise PinError(f"existing {tool} pin is not a symlink")
            else:
                previous[tool] = None

        try:
            for tool in TOOLS:
                atomic_link(target_bin / tool, sources[tool])
            safe_path = f"{target_bin}:{SAFE_PATH_SUFFIX}"
            observed = verify_runtime(
                expected, {tool: tool for tool in TOOLS}, safe_path, home
            )
        except Exception:
            for tool in TOOLS:
                restore(target_bin / tool, previous[tool])
            raise
    finally:
        os.close(descriptor)

    return observed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True, type=Path)
    parser.add_argument("--runtime-bin", required=True, type=Path)
    args = parser.parse_args()
    try:
        home_raw = os.environ.get("HOME", "")
        if not home_raw or not Path(home_raw).is_absolute():
            raise PinError("HOME must be absolute")
        home = Path(home_raw)
        observed = pin(args.product, args.runtime_bin, home)
    except (OSError, RuntimeError, PinError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(
        {
            **observed,
            "path": str(home / ".factory/bin"),
            "schema": "nysa.software-factory.owner-runtime-pin/v1",
            "status": "ready",
        },
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
