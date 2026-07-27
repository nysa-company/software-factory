#!/usr/bin/env python3
"""Fail-closed protected-main conflict ownership policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess


def git(work: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(work), *arguments], text=True
    ).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    if receipt.get("ticket") != args.ticket or receipt.get("repair_owner") != "builder":
        raise SystemExit(1)
    match = re.search(
        r"(?m)^TEST_PATHS=(.*)$", args.project.read_text(encoding="utf-8")
    )
    tests = shlex.split(match.group(1)) if match else []
    safe = re.compile(r"[A-Za-z0-9._/@+-]+")
    old = receipt["checkpoint_base_sha"]
    sealed = f"refs/retry/{args.ticket}"
    changed = {}
    for line in git(args.workdir, "diff", "--name-status", f"{old}..{sealed}").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            changed[parts[-1]] = parts[0]
    for path in args.paths:
        if not safe.fullmatch(path):
            raise SystemExit(1)
        if path in {
            f"factory/tickets/{args.ticket}.md",
            f"factory/tickets/{args.ticket}-bundle.md",
        }:
            print("theirs", path, sep="\t")
            continue
        basename = PurePosixPath(path).name
        if (
            path.startswith(("factory/", "context/", ".github/", "scripts/"))
            or any(
                path == prefix or path.startswith(prefix.rstrip("/") + "/")
                for prefix in tests
            )
            or basename in {
                "Dockerfile", "compose.yaml", "docker-compose.yml", "package.json",
                "package-lock.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
                "turbo.json", "yarn.lock",
            }
            or (
                "/" not in path
                and PurePosixPath(path).suffix in {".json", ".toml", ".yaml", ".yml"}
            )
            or basename.startswith(".")
            or basename.startswith("tsconfig")
            or ".config." in basename
            or changed.get(path, "") not in {"A", "M", "D"}
        ):
            raise SystemExit(1)
        for ref in (
            receipt["old_protected_base_sha"],
            receipt["new_protected_base_sha"],
            sealed,
        ):
            entry = git(args.workdir, "ls-tree", ref, "--", path)
            if entry and entry.split()[0] in {"120000", "160000"}:
                raise SystemExit(1)
        print("ours", path, sep="\t")


if __name__ == "__main__":
    main()
