#!/usr/bin/env python3
"""Opt-in six-container concurrency canary for the isolated provider executor."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    "ubuntu:24.04@sha256:"
    "4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90"
)
SCHEMA = "nysa.software-factory.provider-execution-request/v2"


def main() -> None:
    if os.environ.get("FACTORY_RUN_DOCKER_CANARY") != "1":
        raise SystemExit(
            "set FACTORY_RUN_DOCKER_CANARY=1 to run the six-container canary"
        )
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix="provider-docker-canary.") as temporary:
        root = Path(temporary).resolve()
        source = root / "source"
        source.mkdir()
        (source / "README.md").write_text("isolated canary\n", encoding="utf-8")
        input_path = root / "input.json"
        input_path.write_text('{"kind":"six-way-canary"}\n', encoding="utf-8")
        attempts = root / "attempts"
        policy = root / "provider-policy.json"
        policy_value = {
            "schema": "factory-provider-concurrency-policy/v1",
            "coupled_max_concurrent": 6,
            "global": {
                "max_concurrent": 6,
                "max_starts": 12,
                "window_seconds": 60,
            },
            "provider_families": {
                "mock": {
                    "max_concurrent": 6,
                    "max_starts": 12,
                    "window_seconds": 60,
                },
            },
            "account_routes": {
                "local": {
                    "max_concurrent": 6,
                    "max_starts": 12,
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
        for number in range(1, 8):
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
                    "--budget-day", "2026-07-20",
                    "--product-daily-cap-micro-usd", "1000000",
                    "--ticket-cap-micro-usd", "1000000",
                    "--machine-daily-cap-micro-usd", "1000000",
                    "--memory",
                    "128m",
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
        with ThreadPoolExecutor(max_workers=7) as pool:
            results = list(pool.map(execute, requests))
        elapsed = time.monotonic() - started
        if elapsed >= 20:
            raise RuntimeError(f"six-container wave was unexpectedly slow: {elapsed:.2f}s")
        admitted = [item for item in results if item["admitted"]]
        denied = [item for item in results if not item["admitted"]]
        if len(admitted) != 6 or len(denied) != 1 or any(
            item["execution"]["mode"] != "isolated-v1"
            or item["execution"]["return_code"] != 0
            for item in admitted
        ):
            raise RuntimeError("six-container wave returned invalid results")
        if len({item["execution"]["container_name"] for item in admitted}) != 6:
            raise RuntimeError("container identities were not unique")
        print(
            "PASS: six isolated containers completed concurrently and the "
            f"seventh was denied in {elapsed:.2f}s"
        )


if __name__ == "__main__":
    main()
