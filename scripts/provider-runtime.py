#!/usr/bin/env python3
"""Couple transactional admission to one isolated provider execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request


OUTPUT_SCHEMA = "nysa.software-factory.provider-runtime/v1"
REQUEST_SCHEMA = "nysa.software-factory.provider-execution-request/v3"
MAX_JSON = 1_000_000


class RuntimeError(ValueError):
    pass


class BrokerSettledError(RuntimeError):
    def __init__(self, message: str, *, cancelled: bool = False):
        super().__init__(message)
        self.cancelled = cancelled


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_absolute():
        raise RuntimeError(f"{label} path must be absolute")
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_mode & 0o022
        or before.st_size > MAX_JSON
    ):
        raise RuntimeError(f"{label} is unsafe")
    raw = path.read_bytes()
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev, after.st_ino, after.st_size
    ):
        raise RuntimeError(f"{label} changed while reading")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain an object")
    return value


def policy_hash(path: Path) -> str:
    value = read_json(path, "provider policy")
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def command_json(command: list[str], label: str) -> dict[str, Any]:
    result = subprocess.run(
        command, text=True, capture_output=True, check=False, timeout=1200
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{label} returned invalid JSON: {result.stderr.strip()}"
        ) from error
    if result.returncode:
        message = value.get("error") if isinstance(value, dict) else None
        raise RuntimeError(f"{label} failed: {message or result.stderr.strip()}")
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned a non-object")
    return value


def coordinator(args: argparse.Namespace, *arguments: str) -> dict[str, Any]:
    return command_json(
        [
            sys.executable, str(args.coordinator), "--db", str(args.db),
            *arguments,
        ],
        "provider coordinator",
    )


def operation(attempt_id: str, step: str) -> str:
    return f"{attempt_id}:{step}"


def write_exclusive(path: Path, raw: bytes) -> None:
    if not path.is_absolute():
        raise RuntimeError("worker input path must be absolute")
    parent = path.parent
    info = parent.lstat()
    if (
        parent.resolve(strict=True) != parent
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise RuntimeError("worker input directory is unsafe")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def response_text(value: dict[str, Any]) -> str:
    choices = value.get("choices")
    if isinstance(choices, list) and len(choices) == 1:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    content = value.get("content")
    if isinstance(content, list):
        texts = [
            item.get("text")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        if texts:
            return "".join(texts)
    output = value.get("output")
    if isinstance(output, list):
        texts = []
        for item in output:
            if not isinstance(item, dict) or not isinstance(item.get("content"), list):
                continue
            for block in item["content"]:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    texts.append(block["text"])
        if texts:
            return "".join(texts)
    raise RuntimeError("provider response has no supported text result")


def usage_value(value: dict[str, Any], *names: str) -> int:
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return 0
    for name in names:
        selected = usage.get(name)
        if isinstance(selected, int) and not isinstance(selected, bool) and selected >= 0:
            return selected
    return 0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def prove_broker_drained(
    broker_base: list[str], attempt_id: str, timeout: float
) -> None:
    deadline = time.monotonic() + min(max(timeout, 1), 125)
    while True:
        report = command_json(
            [*broker_base, "status", "--attempt-id", attempt_id],
            "provider credential broker status",
        )
        tokens = report.get("tokens")
        if tokens == [] or (
            isinstance(tokens, list)
            and len(tokens) == 1
            and tokens[0].get("active") is False
            and tokens[0].get("request_in_flight") is False
        ):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "provider request drain was not proven; reservation retained"
            )
        time.sleep(0.5)


def broker_worker_input(
    args: argparse.Namespace, request: dict[str, Any], attempt_id: str
) -> None:
    if not all(
        (
            args.broker_db,
            args.broker_credentials,
            args.broker_url,
            args.broker_path,
            args.broker_model,
            args.provider_request,
        )
    ):
        raise BrokerSettledError(
            "brokered provider execution is incompletely configured"
        )
    try:
        provider_request = read_json(args.provider_request, "provider request")
        if provider_request.get("model") != args.broker_model:
            raise RuntimeError("provider request model is not broker-bound")
    except RuntimeError as error:
        raise BrokerSettledError(str(error)) from error
    broker_base = [
        sys.executable,
        str(args.credential_broker),
        "--db", str(args.broker_db),
        "--credentials", str(args.broker_credentials),
    ]
    if args.broker_allow_http_loopback:
        broker_base.append("--allow-http-loopback")
    issued = False
    previous_signals: dict[int, Any] = {}

    def interrupted(signum, _frame):
        raise BrokerSettledError(
            f"broker request cancelled by signal {signum}", cancelled=True
        )

    for selected in (signal.SIGTERM, signal.SIGINT):
        previous_signals[selected] = signal.signal(selected, interrupted)
    try:
        issuance = command_json(
            [
                *broker_base,
                "issue",
                "--attempt-id", attempt_id,
                "--route-id", request["route_id"],
                "--model", args.broker_model,
                "--reserve-micro-usd", str(args.reserve_micro_usd),
                "--ttl-seconds", str(args.broker_ttl_seconds),
                "--max-requests", "1",
            ],
            "provider credential broker issuance",
        )
        token = issuance.get("broker_token")
        if not isinstance(token, str):
            raise RuntimeError("provider credential broker returned no token")
        issued = True
        started = time.monotonic()
        http_request = urllib.request.Request(
            args.broker_url.rstrip("/") + args.broker_path,
            data=canonical(provider_request).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        context = None
        if args.broker_ca:
            import ssl
            context = ssl.create_default_context(cafile=str(args.broker_ca))
        opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=context)
        )
        with opener.open(http_request, timeout=args.broker_timeout) as response:
            raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise RuntimeError("provider response exceeds runtime limit")
        try:
            provider_response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("provider response is invalid JSON") from error
        if not isinstance(provider_response, dict):
            raise RuntimeError("provider response must be an object")
        try:
            mutation = json.loads(response_text(provider_response))
        except json.JSONDecodeError as error:
            raise RuntimeError("provider response mutation is invalid JSON") from error
        if (
            not isinstance(mutation, dict)
            or set(mutation) != {"files", "patch"}
            or not isinstance(mutation["patch"], str)
            or not isinstance(mutation["files"], list)
        ):
            raise RuntimeError("provider response mutation schema is invalid")
        worker_input = {
            "files": mutation["files"],
            "patch": mutation["patch"],
            "schema": "nysa.software-factory.provider-worker-input/v1",
            "telemetry": {
                "charge_micro_usd": args.reserve_micro_usd,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "input_tokens": usage_value(
                    provider_response, "input_tokens", "prompt_tokens"
                ),
                "output_tokens": usage_value(
                    provider_response, "output_tokens", "completion_tokens"
                ),
                "provider_request_id": None,
            },
        }
        write_exclusive(
            Path(request["input"]),
            (canonical(worker_input) + "\n").encode("utf-8"),
        )
    except BrokerSettledError:
        raise
    except Exception as error:
        raise BrokerSettledError(f"broker request failed: {error}") from error
    finally:
        try:
            revocation = command_json(
                [
                    *broker_base,
                    "revoke",
                    "--attempt-id", attempt_id,
                ],
                "provider credential broker revocation",
            )
            if issued and revocation.get("revoked") is not True:
                raise RuntimeError(
                    "provider credential broker did not prove token revocation"
                )
            prove_broker_drained(broker_base, attempt_id, args.broker_timeout)
        finally:
            for selected, previous in previous_signals.items():
                signal.signal(selected, previous)


def execute(args: argparse.Namespace) -> dict[str, Any]:
    request = read_json(args.request, "execution request")
    if request.get("schema") != REQUEST_SCHEMA:
        raise RuntimeError("execution request schema is unsupported")
    attempt_id = request.get("attempt_id")
    if not isinstance(attempt_id, str):
        raise RuntimeError("execution request attempt_id is invalid")
    expected_policy = policy_hash(args.policy)
    if request.get("policy_sha256") != expected_policy:
        raise RuntimeError("execution request is not bound to the active provider policy")

    reservation = coordinator(
        args,
        "reserve",
        "--operation-id", operation(attempt_id, "reserve"),
        "--attempt-id", attempt_id,
        "--provider-family", args.provider_family,
        "--account-route", args.account_route,
        "--reserve-micro-usd", str(args.reserve_micro_usd),
        "--product-id", args.product_id,
        "--ticket-id", request["ticket"],
        "--budget-day", args.budget_day,
        "--product-daily-cap-micro-usd", str(args.product_daily_cap_micro_usd),
        "--ticket-cap-micro-usd", str(args.ticket_cap_micro_usd),
        "--machine-daily-cap-micro-usd", str(args.machine_daily_cap_micro_usd),
        "--policy", str(args.policy),
    )
    if not reservation.get("admitted"):
        return {
            "admitted": False,
            "attempt_id": attempt_id,
            "denials": reservation.get("denials", []),
            "schema": OUTPUT_SCHEMA,
        }
    coordinator(
        args,
        "mark-go",
        "--operation-id", operation(attempt_id, "go"),
        "--attempt-id", attempt_id,
        "--expected-version", "2",
    )
    coordinator(
        args,
        "mark-submitted",
        "--operation-id", operation(attempt_id, "submitted"),
        "--attempt-id", attempt_id,
        "--expected-version", "3",
    )
    if args.provider_transport == "broker":
        try:
            broker_worker_input(args, request, attempt_id)
        except BrokerSettledError as error:
            result = "cancelled" if error.cancelled else "failed"
            if error.cancelled:
                coordinator(
                    args,
                    "request-cancel",
                    "--operation-id", operation(attempt_id, "broker-cancel-request"),
                    "--attempt-id", attempt_id,
                    "--expected-version", "4",
                    "--reason", "controller_signal",
                )
            coordinator(
                args,
                "terminalize",
                "--operation-id", operation(attempt_id, "broker-terminal"),
                "--attempt-id", attempt_id,
                "--expected-version", "4",
                "--result", result,
                "--charge-micro-usd", str(args.reserve_micro_usd),
            )
            raise

    executor_command = [
        sys.executable,
        str(args.executor),
        "--runtime", args.container_runtime,
        "--attempt-root", str(args.attempt_root),
        "--runtime-timeout", str(args.runtime_timeout),
        "execute",
        "--mode", "isolated-v1",
        "--request", str(args.request),
        "--timeout", str(args.timeout),
        "--memory", args.memory,
        "--cpus", str(args.cpus),
        "--pids-limit", str(args.pids_limit),
    ]
    execution = command_json(executor_command, "provider executor")
    application = None
    charge_micro_usd = args.reserve_micro_usd
    if execution.get("return_code") == 0 and args.artifact_mode == "patch-v1":
        if not all(
            (
                args.worktree,
                args.artifact_policy,
                args.apply_lock,
                args.expected_branch,
            )
        ):
            raise RuntimeError("patch-v1 artifact handling is incompletely configured")
        application = command_json(
            [
                sys.executable,
                str(args.artifact_controller),
                "--attempt", str(args.attempt_root / attempt_id),
                "--worktree", str(args.worktree),
                "--policy", str(args.artifact_policy),
                "--lock", str(args.apply_lock),
                "--expected-branch", args.expected_branch,
                "--base-sha", request["base_sha"],
                "--reserve-micro-usd", str(args.reserve_micro_usd),
                "apply",
            ],
            "provider artifact controller",
        )
        charge_micro_usd = application["charge_micro_usd"]
    terminal_result = (
        "succeeded"
        if execution.get("return_code") == 0
        and (args.artifact_mode != "patch-v1" or application is not None)
        else "failed"
    )
    terminal = coordinator(
        args,
        "terminalize",
        "--operation-id", operation(attempt_id, "terminal"),
        "--attempt-id", attempt_id,
        "--expected-version", "4",
        "--result", terminal_result,
        "--charge-micro-usd", str(charge_micro_usd),
    )
    return {
        "admitted": True,
        "attempt_id": attempt_id,
        "execution": execution,
        "application": application,
        "schema": OUTPUT_SCHEMA,
        "terminal": terminal,
    }


def cancel(args: argparse.Namespace) -> dict[str, Any]:
    status = coordinator(args, "status", "--attempt-id", args.attempt_id)
    attempts = status.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise RuntimeError("coordinator returned an invalid attempt status")
    attempt = attempts[0]
    if attempt.get("state") == "terminal":
        return {
            "attempt_id": args.attempt_id,
            "cancelled": attempt.get("terminal_result") == "cancelled",
            "schema": OUTPUT_SCHEMA,
            "terminal": attempt,
        }
    if attempt.get("state") != "submitted":
        raise RuntimeError(
            f"attempt cancellation requires submitted state, got {attempt.get('state')}"
        )
    coordinator(
        args,
        "request-cancel",
        "--operation-id", operation(args.attempt_id, "cancel-request"),
        "--attempt-id", args.attempt_id,
        "--expected-version", str(attempt["version"]),
        "--reason", "operator_requested",
    )
    executor_command = [
        sys.executable,
        str(args.executor),
        "--runtime", args.container_runtime,
        "--attempt-root", str(args.attempt_root),
        "--runtime-timeout", str(args.runtime_timeout),
        "cancel",
        "--attempt-id", args.attempt_id,
    ]
    if args.binding_sha256:
        executor_command.extend(["--binding-sha256", args.binding_sha256])
    cancellation = command_json(executor_command, "provider executor cancellation")
    if cancellation.get("removed") is not True:
        raise RuntimeError(
            "container termination was not proven; reservation and slot retained"
        )
    terminal = coordinator(
        args,
        "terminalize",
        "--operation-id", operation(args.attempt_id, "cancelled"),
        "--attempt-id", args.attempt_id,
        "--expected-version", str(attempt["version"]),
        "--result", "cancelled",
        "--charge-micro-usd", str(attempt["reserve_micro_usd"]),
    )
    return {
        "attempt_id": args.attempt_id,
        "cancellation": cancellation,
        "cancelled": True,
        "schema": OUTPUT_SCHEMA,
        "terminal": terminal,
    }


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent
    value = argparse.ArgumentParser()
    value.add_argument("--db", required=True, type=Path)
    value.add_argument("--policy", required=True, type=Path)
    value.add_argument(
        "--coordinator", type=Path, default=root / "provider-coordinator.py"
    )
    value.add_argument("--executor", type=Path, default=root / "provider-executor.py")
    value.add_argument(
        "--artifact-controller",
        type=Path,
        default=root / "provider-artifact-controller.py",
    )
    value.add_argument(
        "--credential-broker",
        type=Path,
        default=root / "provider-credential-broker.py",
    )
    value.add_argument("--container-runtime", default="docker")
    command = value.add_subparsers(dest="command", required=True)
    run = command.add_parser("execute")
    run.add_argument("--request", required=True, type=Path)
    run.add_argument("--attempt-root", required=True, type=Path)
    run.add_argument("--provider-family", required=True)
    run.add_argument("--account-route", required=True)
    run.add_argument("--reserve-micro-usd", required=True, type=int)
    run.add_argument("--product-id", required=True)
    run.add_argument("--budget-day", required=True)
    run.add_argument("--product-daily-cap-micro-usd", required=True, type=int)
    run.add_argument("--ticket-cap-micro-usd", required=True, type=int)
    run.add_argument("--machine-daily-cap-micro-usd", required=True, type=int)
    run.add_argument("--runtime-timeout", type=float, default=30)
    run.add_argument("--timeout", type=float, default=900)
    run.add_argument("--memory", default="1g")
    run.add_argument("--cpus", type=float, default=1)
    run.add_argument("--pids-limit", type=int, default=128)
    run.add_argument("--artifact-mode", choices=("generic", "patch-v1"), default="generic")
    run.add_argument("--worktree", type=Path)
    run.add_argument("--artifact-policy", type=Path)
    run.add_argument("--apply-lock", type=Path)
    run.add_argument("--expected-branch")
    run.add_argument(
        "--provider-transport", choices=("prepared-input", "broker"),
        default="prepared-input",
    )
    run.add_argument("--broker-db", type=Path)
    run.add_argument("--broker-credentials", type=Path)
    run.add_argument("--broker-url")
    run.add_argument("--broker-path")
    run.add_argument("--broker-model")
    run.add_argument("--broker-ca", type=Path)
    run.add_argument("--broker-ttl-seconds", type=int, default=900)
    run.add_argument("--broker-timeout", type=float, default=900)
    run.add_argument("--provider-request", type=Path)
    run.add_argument(
        "--broker-allow-http-loopback", action="store_true",
        help=argparse.SUPPRESS,
    )
    run.set_defaults(handler=execute)
    cancellation = command.add_parser("cancel")
    cancellation.add_argument("--attempt-id", required=True)
    cancellation.add_argument("--attempt-root", required=True, type=Path)
    cancellation.add_argument("--binding-sha256")
    cancellation.add_argument("--runtime-timeout", type=float, default=30)
    cancellation.set_defaults(handler=cancel)
    return value


def main() -> None:
    try:
        args = parser().parse_args()
        result = args.handler(args)
        print(canonical(result))
    except (
        RuntimeError, OSError, subprocess.SubprocessError, ValueError
    ) as error:
        print(canonical({
            "error": str(error), "schema": OUTPUT_SCHEMA, "status": "error",
        }))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
