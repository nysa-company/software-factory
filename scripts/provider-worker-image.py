#!/usr/bin/env python3
"""Verify and report the release-owned digest-pinned worker image."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys


SCHEMA = "nysa.software-factory.provider-worker-image-lock/v1"
IMAGE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    lock_path = root / "worker" / "image-lock.json"
    dockerfile = root / "worker" / "Dockerfile"
    try:
        raw = lock_path.read_text(encoding="utf-8")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {
                "dockerfile_sha256", "image_reference", "schema",
                "worker_program", "worker_sha256",
            }
            or value.get("schema") != SCHEMA
            or raw != canonical(value) + "\n"
            or not isinstance(value["dockerfile_sha256"], str)
            or not SHA256.fullmatch(value["dockerfile_sha256"])
            or not isinstance(value["image_reference"], str)
            or not IMAGE.fullmatch(value["image_reference"])
            or value["worker_program"] != "worker/provider-worker.mjs"
            or not isinstance(value["worker_sha256"], str)
            or not SHA256.fullmatch(value["worker_sha256"])
            or hashlib.sha256(dockerfile.read_bytes()).hexdigest()
            != value["dockerfile_sha256"]
            or hashlib.sha256(
                (root / value["worker_program"]).read_bytes()
            ).hexdigest() != value["worker_sha256"]
        ):
            raise ValueError("worker image lock is invalid or drifted")
        print(canonical(value))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(canonical({"error": str(error), "schema": SCHEMA, "status": "error"}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
