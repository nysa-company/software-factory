#!/usr/bin/env python3
"""Opt-in staged concurrency canary for the isolated provider executor."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "scripts/provider-runtime.py"
IMAGE = (
    "node:22-bookworm@sha256:"
    "5647be709086c696ff32edaaf1c70cd26d1da6ab2b39c32f3c7b4c4a31957e37"
)
SCHEMA = "nysa.software-factory.provider-execution-request/v3"


def main() -> None:
    if os.environ.get("FACTORY_RUN_DOCKER_CANARY") != "1":
        raise SystemExit(
            "set FACTORY_RUN_DOCKER_CANARY=1 to run the staged container canary"
        )
    capacity = int(os.environ.get("FACTORY_CANARY_CAPACITY", "4"))
    if capacity not in (1, 2, 4, 6):
        raise SystemExit("FACTORY_CANARY_CAPACITY must be 1, 2, 4, or 6")
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix="provider-docker-canary.") as temporary:
        root = Path(temporary).resolve()
        source = root / "source"
        source.mkdir()
        (source / "README.md").write_text("isolated canary\n", encoding="utf-8")
        input_path = root / "input.json"
        input_path.write_text('{"kind":"staged-canary"}\n', encoding="utf-8")
        worker = root / "worker"
        worker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        worker_sha256 = hashlib.sha256(worker.read_bytes()).hexdigest()
        attempts = root / "attempts"
        policy = root / "provider-policy.json"
        policy_value = {
            "schema": "factory-provider-concurrency-policy/v1",
            "coupled_max_concurrent": capacity,
            "global": {
                "max_concurrent": capacity,
                "max_starts": capacity * 2 + 2,
                "window_seconds": 60,
            },
            "provider_families": {
                "mock": {
                    "max_concurrent": capacity,
                    "max_starts": capacity * 2 + 2,
                    "window_seconds": 60,
                },
            },
            "account_routes": {
                "local": {
                    "max_concurrent": capacity,
                    "max_starts": capacity * 2 + 2,
                    "window_seconds": 60,
                },
            },
        }
        policy_canonical = json.dumps(
            policy_value, sort_keys=True, separators=(",", ":")
        )
        policy.write_text(policy_canonical + "\n", encoding="utf-8")
        policy_sha256 = hashlib.sha256(policy_canonical.encode("utf-8")).hexdigest()
        database = root / "state-v2.sqlite3"
        requests = []
        for number in range(1, capacity + 2):
            request = root / f"request-{number}.json"
            request.write_text(
                json.dumps(
                    {
                        "attempt_id": f"canary-{number}",
                        "base_sha": f"{number:040x}",
                        "command": [
                            "/bin/sh",
                            "-c",
                            "mkdir -p /workspace/artifacts; "
                            "cp /workspace/payload/identity.json "
                            "/workspace/artifacts/identity.json; "
                            "sleep 5; printf '%s\\n' isolated "
                            "> /workspace/artifacts/outcome.txt",
                        ],
                        "image": IMAGE,
                        "input": str(input_path),
                        "policy_sha256": policy_sha256,
                        "role": "builder",
                        "route_id": "mock-local",
                        "schema": SCHEMA,
                        "source": str(source),
                        "ticket": f"T-{900 + number}",
                        "worker_program": str(worker),
                        "worker_sha256": worker_sha256,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            requests.append(request)

        def execute(request: Path) -> dict:
            result = subprocess.run(
                [
                    "python3",
                    str(RUNTIME),
                    "--db", str(database),
                    "--policy", str(policy),
                    "execute",
                    "--request", str(request),
                    "--attempt-root", str(attempts),
                    "--provider-family", "mock",
                    "--account-route", "local",
                    "--reserve-micro-usd", "1000",
                    "--product-id", "canary",
                    "--budget-day", datetime.datetime.now(
                        datetime.timezone.utc
                    ).date().isoformat(),
                    "--product-daily-cap-micro-usd", "1000000",
                    "--ticket-cap-micro-usd", "1000000",
                    "--machine-daily-cap-micro-usd", "1000000",
                    "--memory",
                    "512m",
                    "--cpus",
                    "0.5",
                    "--timeout",
                    "30",
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=45,
            )
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout)
            return json.loads(result.stdout)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=capacity + 1) as pool:
            results = list(pool.map(execute, requests))
        elapsed = time.monotonic() - started
        if elapsed >= 20:
            raise RuntimeError(
                f"{capacity}-container wave was unexpectedly slow: {elapsed:.2f}s"
            )
        admitted = [item for item in results if item["admitted"]]
        denied = [item for item in results if not item["admitted"]]
        if len(admitted) != capacity or len(denied) != 1 or any(
            item["execution"]["mode"] != "isolated-v1"
            or item["execution"]["return_code"] != 0
            for item in admitted
        ):
            raise RuntimeError(f"{capacity}-container wave returned invalid results")
        if len({item["execution"]["container_name"] for item in admitted}) != capacity:
            raise RuntimeError("container identities were not unique")
        print(
            f"PASS: {capacity} isolated containers completed concurrently and "
            f"attempt {capacity + 1} was denied in {elapsed:.2f}s"
        )


if __name__ == "__main__":
    main()
