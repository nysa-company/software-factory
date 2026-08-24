#!/usr/bin/env python3
"""Atomically select, prepare, and claim one deterministic Ready ticket."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from effective_ticket import (  # noqa: E402
    authoritative_operator_fields,
    apply_operator_fields,
    load_mapping,
    ticket_branch_prefix,
)
from legacy_closeout import (  # noqa: E402
    ValidationError,
    protected_dependency,
    protected_terminal,
)
import operator_receipt  # noqa: E402


SCHEMA = "nysa.software-factory.dispatch-plan/v1"
COHORT_TRANSACTION_SCHEMA = "nysa.software-factory.qualification-claim/v1"
QUALIFICATION_SCHEMA = "nysa.software-factory.qualification/v1"
QUALIFICATION_SCHEMA_V2 = "nysa.software-factory.qualification/v2"
QUALIFICATION_CONTRACTS = frozenset({"1.8.0", "2.0.0"})
PREPROVIDER_RESET_SCHEMA = "nysa.software-factory.preprovider-branch-resets/v1"
QUALIFICATION_RESET_SCHEMA = "nysa.software-factory.preprovider-branch-resets/v2"
TICKET = re.compile(r"^T-([0-9]+)$")
SHA = re.compile(r"^[0-9a-f]{40}$")
PRIORITY = {"urgent": 0, "high": 1, "normal": 2, "low": 3, "none": 4}


class DispatchError(ValueError):
    pass


class ResetAuthorization(NamedTuple):
    head: str
    source_factory_sha: str = ""
    source_generation: int = 0
    source_product_sha: str = ""
    prior: ResetAuthorization | None = None


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def durable_write(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(canonical(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def cohort_transaction_path(
    worktree_root: Path, qualification_state: dict[str, Any],
) -> Path:
    return worktree_root / (
        f".qualification-claim-{qualification_state['factory_sha']}-"
        f"{qualification_state['generation']}.json"
    )


def cohort_transaction_digest(value: dict[str, Any]) -> str:
    body = {key: item for key, item in value.items() if key != "transaction_sha256"}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def read_cohort_transaction(
    path: Path, qualification_state: dict[str, Any], protected_main: str,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = json.loads(safe_file(path, "qualification claim transaction", 100_000))
    if (
        not isinstance(value, dict)
        or set(value) != {
            "factory_sha", "generation", "operator_map_sha256", "protected_main",
            "protected_tree", "qualification_sha256", "remote_heads", "schema",
            "tickets", "transaction_sha256",
        }
        or value.get("schema") != COHORT_TRANSACTION_SCHEMA
        or value.get("factory_sha") != qualification_state["factory_sha"]
        or value.get("generation") != qualification_state["generation"]
        or value.get("protected_main") != protected_main
        or not SHA.fullmatch(value.get("protected_tree", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("operator_map_sha256", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("qualification_sha256", ""))
        or not isinstance(value.get("tickets"), list)
        or not value["tickets"]
        or any(
            not isinstance(item, dict)
            or not TICKET.fullmatch(item.get("ticket", ""))
            for item in value["tickets"]
        )
        or len({item["ticket"] for item in value["tickets"]}) != len(value["tickets"])
        or not isinstance(value.get("remote_heads"), dict)
        or any(
            not isinstance(branch, str)
            or not isinstance(head, str)
            or head and not SHA.fullmatch(head)
            for branch, head in value["remote_heads"].items()
        )
        or value.get("transaction_sha256") != cohort_transaction_digest(value)
    ):
        raise DispatchError("qualification claim transaction is invalid")
    return value


def remote_branch_heads(
    product: Path, remote: str, branches: list[str],
) -> dict[str, str]:
    refs = {f"refs/heads/{branch}": branch for branch in branches}
    result = {branch: "" for branch in branches}
    output = git(product, "ls-remote", "--heads", "--", remote, *refs)
    for line in output.splitlines():
        values = line.split()
        if len(values) != 2 or values[1] not in refs or result[refs[values[1]]]:
            raise DispatchError("qualification ticket remote branches are ambiguous")
        if not SHA.fullmatch(values[0]):
            raise DispatchError("qualification ticket remote branch is invalid")
        result[refs[values[1]]] = values[0]
    return result


def git(root: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if check and result.returncode:
        raise DispatchError(result.stderr.strip() or "Git operation failed")
    return result.stdout


def git_succeeds(root: Path, *arguments: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    ).returncode == 0


def certified_origin(product: Path) -> str:
    expected = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    if not expected or any(character in expected for character in "\n\r\t"):
        raise DispatchError("certified product origin is unavailable")
    urls = [
        line for line in git(product, "remote", "get-url", "--push", "--all", "origin").splitlines()
        if line
    ]
    if urls != [expected]:
        raise DispatchError("product origin does not match certification")
    return expected


def safe_directory(path: Path, label: str, owner_only: bool = False) -> None:
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or (owner_only and info.st_mode & 0o077)
    ):
        raise DispatchError(f"{label} is unsafe")


def safe_file(path: Path, label: str, maximum: int = 5_000_000) -> str:
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or info.st_size > maximum
    ):
        raise DispatchError(f"{label} is unsafe")
    return path.read_text(encoding="utf-8")


def field(text: str, name: str, default: str = "") -> str:
    values = re.findall(
        rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.MULTILINE | re.IGNORECASE
    )
    if len(values) > 1:
        raise DispatchError(f"ticket contains duplicate {name}")
    return values[0] if values else default


def capacity(factory: Path) -> int:
    descriptor = safe_file(factory / "PROJECT.env", "project descriptor", 100_000)
    values = []
    for raw in descriptor.splitlines():
        line = re.sub(r"^\s*export\s+", "", raw.strip())
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"MAX_CONCURRENT_TICKETS\s*=\s*['\"]?([0-9]+)['\"]?", line)
        if match:
            values.append(int(match.group(1)))
    if len(values) > 1:
        raise DispatchError("MAX_CONCURRENT_TICKETS is ambiguous")
    selected = values[0] if values else 4
    if not 1 <= selected <= 4:
        raise DispatchError("MAX_CONCURRENT_TICKETS is invalid")
    return selected


def dependencies(text: str) -> tuple[str, ...]:
    raw = field(text, "Depends-On", "none")
    if raw.lower() == "none":
        return ()
    values = tuple(item.strip() for item in raw.split(","))
    if (
        not values
        or any(not TICKET.fullmatch(item) for item in values)
        or len(values) != len(set(values))
    ):
        raise DispatchError("ticket dependencies are invalid")
    return values


def qualification_terminal(
    product: Path, tickets: list[str], graph: dict[str, tuple[str, ...]],
) -> set[str]:
    selected = set(tickets)
    terminal = set()
    for ticket in selected:
        try:
            protected_terminal(product, ticket)
        except ValidationError:
            continue
        terminal.add(ticket)
    for ticket in set().union(*(set(items) for items in graph.values())) - selected:
        try:
            protected_dependency(product, ticket)
        except ValidationError:
            continue
        terminal.add(ticket)
    return terminal


def qualification(
    product: Path, factory: Path, configured_capacity: int
) -> dict[str, Any] | None:
    path = factory / "QUALIFICATION.json"
    if not path.exists():
        return None
    value = json.loads(safe_file(path, "qualification manifest", 100_000))
    if value.get("schema") == QUALIFICATION_SCHEMA_V2:
        fresh_keys = {
            "budget_usd", "capacity", "contract_version", "factory_sha",
            "generation", "per_run_budget_usd", "per_ticket_budget_usd",
            "schema", "target_done", "tickets",
        }
        successor = value.get("mode") == "successor"
        budget_profile = (
            value.get("budget_usd"),
            value.get("per_ticket_budget_usd"),
            value.get("per_run_budget_usd"),
        )
        extended = budget_profile == (
            "300.000000", "100.000000", "10.000000",
        )
        keys = fresh_keys | ({"mode", "source_factory_sha"} if successor else set())
        tickets = value.get("tickets")
        target_done = value.get("target_done")
        selected_capacity = value.get("capacity")
        pin = safe_file(factory / "KIT_PIN", "kit pin", 100).strip()
        if (
            set(value) != keys
            or value.get("contract_version") not in QUALIFICATION_CONTRACTS
            or value.get("factory_sha") != pin
            or not isinstance(value.get("generation"), int)
            or isinstance(value.get("generation"), bool)
            or value["generation"] < 1
            or not isinstance(tickets, list)
            or not isinstance(target_done, int)
            or isinstance(target_done, bool)
            or target_done not in (1, 3, 4)
            or len(tickets) != target_done
            or any(
                not isinstance(item, str) or not TICKET.fullmatch(item)
                for item in tickets
            )
            or len(tickets) != len(set(tickets))
            or not isinstance(selected_capacity, int)
            or isinstance(selected_capacity, bool)
            or selected_capacity not in (3, 4)
            or target_done == 1 and selected_capacity != 3
            or target_done > selected_capacity
            or (
                successor
                and (
                    target_done != 3
                    or selected_capacity != 3
                    or not SHA.fullmatch(value.get("source_factory_sha", ""))
                    or value["source_factory_sha"] == pin
                    or value.get("budget_usd") != "300.000000"
                    or value.get("per_ticket_budget_usd") != "100.000000"
                    or value.get("per_run_budget_usd") != "10.000000"
                )
            )
            or extended and (target_done != 3 or selected_capacity != 3)
            or (
                not successor
                and budget_profile not in {
                    ("100.000000", "25.000000", "2.000000"),
                    ("300.000000", "100.000000", "10.000000"),
                }
            )
            or configured_capacity != selected_capacity
        ):
            raise DispatchError("Contract 1.8 qualification manifest is invalid")
        graph = {}
        for ticket in tickets:
            text = safe_file(factory / "tickets" / f"{ticket}.md", f"ticket {ticket}")
            graph[ticket] = dependencies(text)
            if ticket in graph[ticket]:
                raise DispatchError("Contract 1.8 qualification ticket depends on itself")
        pending = {
            ticket: {item for item in graph[ticket] if item in graph}
            for ticket in tickets
        }
        while pending:
            ready = {ticket for ticket, items in pending.items() if not items}
            if not ready:
                raise DispatchError("Contract 1.8 qualification dependencies contain a cycle")
            pending = {
                ticket: items - ready
                for ticket, items in pending.items()
                if ticket not in ready
            }
        terminal = qualification_terminal(product, tickets, graph)
        return {
            **value,
            "capacity": selected_capacity,
            "dependencies": graph,
            "done": len(set(tickets) & terminal),
            "terminal": terminal,
        }
    keys = {
        "factory_sha", "final_capacity", "generation", "initial_capacity",
        "ramp_after_done", "schema", "target_done", "tickets",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise DispatchError("qualification manifest fields are invalid")
    tickets = value["tickets"]
    pin = safe_file(factory / "KIT_PIN", "kit pin", 100).strip()
    if (
        value["schema"] != QUALIFICATION_SCHEMA
        or value["factory_sha"] != pin
        or not isinstance(value["generation"], int)
        or isinstance(value["generation"], bool)
        or value["generation"] < 1
        or not isinstance(tickets, list)
        or len(tickets) != 10
        or len(tickets) != len(set(tickets))
        or any(not isinstance(item, str) or not TICKET.fullmatch(item) for item in tickets)
        or value["target_done"] != len(tickets)
        or not all(
            isinstance(value[name], int) and not isinstance(value[name], bool)
            for name in ("initial_capacity", "ramp_after_done", "final_capacity")
        )
        or not 1 < value["initial_capacity"] <= value["final_capacity"] <= configured_capacity
        or not 1 <= value["ramp_after_done"] < value["target_done"]
    ):
        raise DispatchError("qualification manifest is invalid")

    graph: dict[str, tuple[str, ...]] = {}
    for ticket in tickets:
        text = safe_file(factory / "tickets" / f"{ticket}.md", f"ticket {ticket}")
        graph[ticket] = dependencies(text)
        if ticket in graph[ticket]:
            raise DispatchError("qualification ticket depends on itself")
    pending = {ticket: {item for item in graph[ticket] if item in graph} for ticket in tickets}
    while pending:
        ready = {ticket for ticket, items in pending.items() if not items}
        if not ready:
            raise DispatchError("qualification dependencies contain a cycle")
        pending = {
            ticket: items - ready
            for ticket, items in pending.items()
            if ticket not in ready
        }

    terminal = qualification_terminal(product, tickets, graph)
    done = len(set(tickets) & terminal)
    return {
        **value,
        "capacity": (
            value["initial_capacity"]
            if done < value["ramp_after_done"]
            else value["final_capacity"]
        ),
        "dependencies": graph,
        "done": done,
        "terminal": terminal,
    }


def validate_qualification_ticket_sources(
    product: Path, state: dict[str, Any] | None,
) -> None:
    if state is None or state.get("schema") != QUALIFICATION_SCHEMA_V2:
        return
    for ticket in state["tickets"]:
        path = f"factory/tickets/{ticket}.md"
        control = git(product, "rev-parse", f"HEAD:{path}", check=False).strip()
        protected = git(
            product, "rev-parse", f"origin/main:{path}", check=False
        ).strip()
        if not SHA.fullmatch(control) or control != protected:
            raise DispatchError(
                f"{ticket}: qualification ticket source differs from protected dispatch"
            )


def preprovider_reset_authorizations(
    factory: Path, qualification_state: dict[str, Any] | None, prefix: str
) -> dict[str, ResetAuthorization]:
    path = factory / "qualification/preprovider-branch-resets.json"
    if (
        qualification_state is None
        or qualification_state.get("schema") != QUALIFICATION_SCHEMA_V2
    ):
        if not path.exists():
            return {}
        raise DispatchError("pre-provider branch resets require Contract 1.8 qualification")
    product = factory.parent
    relative = "factory/qualification/preprovider-branch-resets.json"
    protected = git(product, "rev-parse", "refs/remotes/origin/main").strip()
    entries = git(product, "ls-tree", protected, "--", relative).strip().split(None, 3)
    local = path.exists() or path.is_symlink()
    if not entries and not local:
        return {}
    if (
        len(entries) != 4
        or entries[:2] != ["100644", "blob"]
        or entries[3] != relative
        or not local
        or git(product, "ls-tree", "HEAD", "--", relative).strip().split(None, 3)
        != entries
    ):
        raise DispatchError(
            "pre-provider branch reset authorization is not protected"
        )
    try:
        size = int(git(product, "cat-file", "-s", entries[2]))
    except ValueError as error:
        raise DispatchError(
            "pre-provider branch reset authorization is invalid"
        ) from error
    if size < 0 or size > 100_000:
        raise DispatchError(
            "pre-provider branch reset authorization is oversized"
        )
    blob = git(product, "cat-file", "blob", entries[2])
    raw = safe_file(path, "pre-provider branch reset authorization", 100_000)
    if raw != blob:
        raise DispatchError(
            "pre-provider branch reset authorization differs from protected main"
        )
    value = json.loads(raw)
    resets = value.get("resets")
    schema = value.get("schema")
    source = value.get("source_qualification")
    if (
        schema not in {PREPROVIDER_RESET_SCHEMA, QUALIFICATION_RESET_SCHEMA}
        or set(value) != (
            {"factory_sha", "resets", "schema"}
            if schema == PREPROVIDER_RESET_SCHEMA
            else {"factory_sha", "resets", "schema", "source_qualification"}
        )
        or value.get("factory_sha") != qualification_state["factory_sha"]
        or not isinstance(resets, list)
        or not resets
    ):
        raise DispatchError("pre-provider branch reset authorization is invalid")
    if schema == QUALIFICATION_RESET_SCHEMA and (
        not isinstance(source, dict)
        or set(source) != {"factory_sha", "generation", "product_sha"}
        or not SHA.fullmatch(source.get("factory_sha", ""))
        or not SHA.fullmatch(source.get("product_sha", ""))
        or not isinstance(source.get("generation"), int)
        or isinstance(source.get("generation"), bool)
        or source["generation"] < 1
        or source["generation"] >= qualification_state["generation"]
    ):
        raise DispatchError("qualification control reset source is invalid")
    result = {}
    for item in resets:
        ticket = item.get("ticket") if isinstance(item, dict) else None
        branch = item.get("branch") if isinstance(item, dict) else None
        head = item.get("head") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or set(item) != {"branch", "head", "ticket"}
            or ticket not in qualification_state["tickets"]
            or branch != prefix + ticket
            or not isinstance(head, str)
            or not SHA.fullmatch(head)
            or ticket in result
        ):
            raise DispatchError("pre-provider branch reset entry is invalid")
        result[ticket] = ResetAuthorization(
            head=head,
            source_factory_sha=(source or {}).get("factory_sha", ""),
            source_generation=(source or {}).get("generation", 0),
            source_product_sha=(source or {}).get("product_sha", ""),
        )
    return result


def operator_mapping(
    path: Path, selected_tickets: list[str] | None = None,
) -> dict[str, Any]:
    raw = safe_file(path, "operator map")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise DispatchError("operator map is invalid")
    if selected_tickets:
        tickets = value.get("tickets")
        if tickets is not None and not isinstance(tickets, dict):
            raise DispatchError("operator map tickets are invalid")
        for ticket in selected_tickets:
            entry = (tickets or {}).get(ticket)
            if entry is None:
                continue
            if (
                not isinstance(entry, dict)
                or entry.get("operator_fields_initialized") is not True
                or (
                    "operator" in entry
                    and not isinstance(entry.get("operator"), dict)
                )
            ):
                raise DispatchError(
                    f"selected-ticket operator projection is invalid: {ticket}"
                )
    return value


def authenticated_prepared_ready_receipts(
    mapping_path: Path,
    state_dir: Path | None,
    qualification_state: dict[str, Any] | None,
) -> dict[str, str]:
    """Return locally authenticated Ready receipts left by partial preparation."""
    if (
        state_dir is None
        or qualification_state is None
        or qualification_state.get("schema") != QUALIFICATION_SCHEMA_V2
        or not state_dir.exists()
    ):
        return {}
    selected = qualification_state.get("tickets")
    if not isinstance(selected, list) or any(
        not isinstance(ticket, str) or not TICKET.fullmatch(ticket)
        for ticket in selected
    ):
        raise DispatchError("qualification tickets are invalid")
    if (
        not mapping_path.is_absolute()
        or state_dir != mapping_path.parent.parent / "controller"
    ):
        raise DispatchError("prepared operator receipt state path is invalid")
    safe_directory(state_dir, "prepared operator receipt state", owner_only=True)
    mapping = operator_mapping(mapping_path, selected)
    receipt_root = state_dir / "operator-receipts"
    if not receipt_root.exists():
        return {}
    safe_directory(receipt_root, "prepared operator receipt root", owner_only=True)
    result: dict[str, str] = {}
    for ticket in selected:
        ticket_root = receipt_root / ticket
        if not ticket_root.exists():
            continue
        safe_directory(ticket_root, "prepared operator ticket receipts", owner_only=True)
        receipts: list[tuple[int, dict[str, Any]]] = []
        for path in ticket_root.iterdir():
            match = re.fullmatch(r"ready-([1-9][0-9]*)[.]json", path.name)
            if path.name.startswith("ready-") and match is None:
                raise DispatchError("prepared operator Ready receipt name is invalid")
            if match is None:
                continue
            try:
                receipt = operator_receipt.safe_receipt(path)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise DispatchError("prepared operator Ready receipt is invalid") from error
            sequence = int(match.group(1))
            if (
                receipt.get("ticket") != ticket
                or receipt.get("action") != "ready"
                or receipt.get("payload") != {}
                or receipt.get("sequence") != sequence
            ):
                raise DispatchError("prepared operator Ready receipt is invalid")
            receipts.append((sequence, receipt))
        if not receipts:
            continue
        receipts.sort(key=lambda item: item[0])
        if [sequence for sequence, _ in receipts] != list(
            range(1, len(receipts) + 1)
        ):
            raise DispatchError("prepared operator Ready receipt sequence is invalid")
        receipt = receipts[-1][1]
        entry = mapping.get("tickets", {}).get(ticket, {})
        success = mapping.get("_sync", {}).get(
            "selected_ticket_success_at", {}
        ).get(ticket)
        pending = entry.get("operator") if isinstance(entry, dict) else None
        if receipt.get("consumed") is True:
            valid = (
                isinstance(receipt.get("consumed_at_epoch"), int)
                and isinstance(success, str)
                and pending is None
            )
        else:
            valid = (
                "consumed_at_epoch" not in receipt
                and pending == {
                    "observed_at": receipt.get("issued_at"),
                    "receipt_sha256": receipt.get("receipt_sha256"),
                    "state": "Ready",
                    "state_base": "backlog",
                }
            )
        if not valid:
            raise DispatchError("prepared operator Ready receipt is not authoritative")
        result[ticket] = receipt["receipt_sha256"]
    return result


def lease_records(directory: Path) -> tuple[set[str], set[str]]:
    tickets: set[str] = set()
    leases: set[str] = set()
    if not directory.exists():
        return tickets, leases
    safe_directory(directory, "dispatcher lease directory")
    for path in sorted(directory.iterdir()):
        raw = safe_file(path, "dispatcher lease")
        value = json.loads(raw)
        ticket = value.get("ticket")
        lease = value.get("lease_id")
        if (
            value.get("schema_version") != 1
            or path.name != f"{ticket}.json"
            or not isinstance(ticket, str)
            or not TICKET.fullmatch(ticket)
            or not isinstance(lease, str)
            or not re.fullmatch(r"[0-9a-f]{64}", lease)
            or not isinstance(value.get("claimed_epoch"), int)
            or isinstance(value.get("claimed_epoch"), bool)
            or not isinstance(value.get("expires_epoch"), int)
            or isinstance(value.get("expires_epoch"), bool)
            or value["expires_epoch"] <= value["claimed_epoch"]
            or ticket in tickets
            or lease in leases
        ):
            raise DispatchError("dispatcher lease state is unsafe")
        tickets.add(ticket)
        leases.add(lease)
    return tickets, leases


def lease_record(directory: Path, ticket: str) -> dict[str, Any] | None:
    path = directory / f"{ticket}.json"
    if not path.exists():
        return None
    value = json.loads(safe_file(path, "dispatcher lease"))
    if (
        not isinstance(value, dict)
        or set(value) != {
            "claimed_epoch", "expires_epoch", "lease_id", "schema_version", "ticket",
        }
        or value.get("schema_version") != 1
        or value.get("ticket") != ticket
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("lease_id", ""))
        or isinstance(value.get("claimed_epoch"), bool)
        or not isinstance(value.get("claimed_epoch"), int)
        or isinstance(value.get("expires_epoch"), bool)
        or not isinstance(value.get("expires_epoch"), int)
        or value["expires_epoch"] <= value["claimed_epoch"]
    ):
        raise DispatchError("dispatcher lease state is unsafe")
    return value


def active_tickets(factory: Path) -> set[str]:
    result = set()
    root = factory / ".active-runs"
    if not root.exists():
        return result
    safe_directory(root, "active-run directory")
    for path in root.iterdir():
        match = re.match(r"^(T-[0-9]+)\.", path.name)
        if match:
            result.add(match.group(1))
    return result


def readiness_executable(product: Path, ticket: str) -> bool:
    try:
        result = subprocess.run(
            [
                sys.executable, "-B",
                str(Path(__file__).with_name("ticket-readiness.py")),
                "--ticket", ticket, "--workdir", str(product),
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "READINESS PASS"


def candidates(
    factory: Path,
    mapping: dict[str, Any],
    excluded: set[str],
    qualification_state: dict[str, Any] | None = None,
):
    result = []
    refusals = []
    tickets = factory / "tickets"
    safe_directory(tickets, "ticket directory")
    pin = safe_file(factory / "KIT_PIN", "kit pin", 100).strip()
    prefix = ticket_branch_prefix(factory)
    refs = set(git(
        factory.parent, "for-each-ref", "--format=%(refname)",
        "refs/heads", "refs/remotes/origin",
    ).splitlines())
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise DispatchError("kit pin is invalid")
    for path in sorted(tickets.glob("T-*.md")):
        match = TICKET.fullmatch(path.stem)
        if not match or path.stem in excluded:
            continue
        if (
            qualification_state is not None
            and path.stem not in qualification_state["tickets"]
        ):
            continue
        text = safe_file(path, f"ticket {path.stem}")
        if field(text, "State").casefold() == "backlog":
            for source in (
                f"refs/remotes/origin/{prefix}{path.stem}",
                f"refs/heads/{prefix}{path.stem}",
            ):
                if source not in refs:
                    continue
                durable = git(
                    factory.parent, "show",
                    f"{source}:factory/tickets/{path.stem}.md", check=False,
                )
                if field(durable, "State").casefold() in {"ready", "canceled"}:
                    text = durable
                    break
                if source.startswith("refs/remotes/"):
                    break
        if field(text, "State").casefold() in {"canceled", "done"}:
            continue
        try:
            ticket_dependencies = dependencies(text)
        except DispatchError:
            if qualification_state is not None:
                raise
            refusals.append({
                "error": "ticket dependencies are invalid",
                "reason_code": "invalid_ticket_contract",
                "ticket": path.stem,
            })
            continue
        operator = authoritative_operator_fields(
            mapping,
            path.stem,
            os.environ.get("FACTORY_RELEASE_CONTRACT_VERSION"),
            os.environ.get("FACTORY_CONTROLLER_STATE_DIR"),
        )
        effective = apply_operator_fields(text, operator)
        if (
            os.environ.get("FACTORY_RELEASE_CONTRACT_VERSION") == "2.0.0"
            and field(text, "State").casefold() == "backlog"
            and field(effective, "State").casefold() == "ready"
        ):
            refusals.append({
                "error": "operator Ready materialization is pending",
                "reason_code": "operator_materialization_pending",
                "ticket": path.stem,
            })
            continue
        ticket_pin = field(effective, "Kit-SHA")
        if ticket_pin and ticket_pin != pin:
            continue
        state = field(effective, "State").lower()
        resumable = (
            operator.get("state_base") == "blocked-escalated"
            and state in ("planning", "building", "review")
        )
        if state != "ready" and not resumable:
            continue
        if qualification_state is not None:
            unresolved = any(
                item not in qualification_state["terminal"]
                for item in ticket_dependencies
            )
        else:
            unresolved = False
            for dependency in ticket_dependencies:
                try:
                    protected_dependency(factory.parent, dependency)
                except ValidationError:
                    unresolved = True
                    break
        if unresolved:
            continue
        if not readiness_executable(factory.parent, path.stem):
            refusals.append({
                "error": "provider-free ticket readiness contract is not executable",
                "reason_code": "invalid_ticket_contract",
                "ticket": path.stem,
            })
            continue
        initiative = field(effective, "Initiative")
        if not re.fullmatch(r"I-[0-9]+", initiative):
            refusals.append({
                "error": "ticket initiative is missing",
                "reason_code": "initiative_missing",
                "ticket": path.stem,
            })
            continue
        priority = field(effective, "Priority", "none").lower()
        if priority not in PRIORITY:
            raise DispatchError(f"ticket {path.stem} priority is invalid")
        result.append(
            (
                PRIORITY[priority],
                int(match.group(1)),
                {
                    "initiative": initiative,
                    "priority": priority,
                    "resumable": resumable,
                    "state": field(effective, "State"),
                    "ticket": path.stem,
                    "depends_on": list(ticket_dependencies),
                },
            )
        )
    return [item[2] for item in sorted(result)], refusals


def worktree_records(product: Path) -> list[dict[str, str]]:
    records = []
    current: dict[str, str] = {}
    for line in git(product, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def release_reset_cell(
    product: Path, worktree_root: Path, branch: str, expected_head: str,
) -> None:
    matches = [
        item for item in worktree_records(product)
        if item.get("branch") == f"refs/heads/{branch}"
    ]
    if not matches:
        return
    if len(matches) != 1:
        raise DispatchError("reset branch is checked out more than once")
    destination = Path(matches[0].get("worktree", "")).resolve(strict=True)
    if destination.parent != worktree_root or not re.fullmatch(
        r"cell-[1-6]", destination.name,
    ):
        raise DispatchError("reset branch is checked out outside a trusted cell")
    safe_directory(destination, "reset worktree")
    if (
        git(
            destination, "status", "--porcelain=v1", "-z",
            "--untracked-files=all", "--ignored", "--ignore-submodules=none",
        )
        or git(destination, "rev-parse", "HEAD").strip() != expected_head
    ):
        raise DispatchError("reset worktree changed before materialization")
    git(product, "worktree", "remove", "--force", str(destination))


def ticket_without_control(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not re.match(r"^(?:State|Kit-SHA|Merge-Policy):", line, re.IGNORECASE)
    ).strip()


def validate_operator_ready_lineage(
    product: Path,
    ticket: str,
    branch: str,
    main: str,
    remote_head: str,
    ticket_path: str,
    plan_path: str,
    expected_receipt_sha256: str = "",
    prior_authorization: ResetAuthorization | str | None = None,
) -> None:
    base = git(product, "merge-base", main, remote_head).strip()
    changed = set(git(
        product, "diff", "--name-only", f"{base}..{remote_head}",
    ).splitlines())
    receipt_paths = [
        path for path in changed
        if re.fullmatch(
            rf"factory/receipts/{re.escape(ticket)}/ready-([1-9][0-9]*)[.]json",
            path,
        )
    ]
    if len(receipt_paths) != 1 or changed != {ticket_path, receipt_paths[0]}:
        raise DispatchError("pre-provider branch is not control-only")
    receipt_path = receipt_paths[0]
    sequence = int(re.search(r"ready-([0-9]+)[.]json$", receipt_path).group(1))
    commits = [
        tuple(line.split("\0"))
        for line in git(
            product, "log", "--first-parent", "--reverse",
            "--format=%H%x00%P%x00%an%x00%ae%x00%s",
            f"{base}..{remote_head}",
        ).splitlines()
    ]
    resets = [
        index for index, item in enumerate(commits)
        if len(item) == 5
        and len(item[1].split()) == 2
        and item[1].split()[1] == base
    ]
    previous = base
    if resets:
        if len(resets) != 1:
            raise DispatchError("pre-provider branch commits are not canonical")
        index = resets[0]
        if index == 0 or index + 1 >= len(commits):
            raise DispatchError("pre-provider branch commits are not canonical")
        merge, reset = commits[index:index + 2]
        parents = merge[1].split()
        if (
            commits[index - 1][0] != parents[0]
            or merge[2:5] != (
                "Software Factory", "factory@local",
                f"Merge commit '{base}' into {branch}",
            )
            or reset[1] != merge[0]
            or reset[2:5] != (
                "Software Factory", "factory@local",
                f"{ticket}: supersede pre-provider control state",
            )
            or git(product, "rev-parse", f"{reset[0]}^{{tree}}").strip()
            != git(product, "rev-parse", f"{base}^{{tree}}").strip()
        ):
            raise DispatchError("pre-provider branch commits are not canonical")
        if (
            prior_authorization is not None
            and parents[0] == reset_authorization(prior_authorization).head
        ):
            validate_preprovider_branch(
                product, ticket, branch, base, prior_authorization, parents[0],
            )
        else:
            validate_operator_ready_lineage(
                product, ticket, branch, base, parents[0], ticket_path,
                plan_path, prior_authorization=prior_authorization,
            )
        previous = reset[0]
        commits = commits[index + 2:]

    materialized = 0
    operator_seen = False
    for commit in commits:
        if len(commit) != 5 or commit[1] != previous:
            raise DispatchError("pre-provider branch commits are not canonical")
        paths = git(
            product, "diff-tree", "--no-commit-id", "--name-only",
            "-r", commit[0],
        ).splitlines()
        if (
            commit[2:5]
            == (
                "Factory Operator", "operator@local",
                f"{ticket}: operator ready receipt {sequence}",
            )
            and paths == [receipt_path]
        ):
            operator_seen = True
        elif (
            operator_seen
            and materialized == 0
            and commit[2:5]
            == (
                "Software Factory", "factory@local",
                f"{ticket}: materialize ticket state",
            )
            and paths == [ticket_path]
        ):
            before = git(product, "show", f"{previous}:{ticket_path}")
            after = git(product, "show", f"{commit[0]}:{ticket_path}")
            expected = re.sub(
                r"^State:\s*Backlog\s*$", "State: Ready", before,
                count=1, flags=re.I | re.M,
            )
            if after != expected:
                raise DispatchError(
                    "pre-provider materialize commit is not canonical"
                )
            materialized = 1
        else:
            raise DispatchError("pre-provider branch commits are not canonical")
        previous = commit[0]

    base_ticket = git(product, "show", f"{base}:{ticket_path}")
    main_ticket = git(product, "show", f"{main}:{ticket_path}")
    remote_ticket = git(product, "show", f"{remote_head}:{ticket_path}")
    try:
        receipt = json.loads(git(product, "show", f"{remote_head}:{receipt_path}"))
    except json.JSONDecodeError as error:
        raise DispatchError("pre-provider operator receipt is invalid") from error
    if (
        not commits
        or materialized != 1
        or ticket_without_control(base_ticket)
        != ticket_without_control(main_ticket)
        or ticket_without_control(base_ticket)
        != ticket_without_control(remote_ticket)
        or field(base_ticket, "State").lower() != "backlog"
        or field(main_ticket, "State").lower() != "backlog"
        or field(remote_ticket, "State").lower() != "ready"
        or field(main_ticket, "Kit-SHA")
        or field(remote_ticket, "Kit-SHA")
        or git_succeeds(product, "cat-file", "-e", f"{main}:{plan_path}")
        or git_succeeds(product, "cat-file", "-e", f"{main}:{receipt_path}")
        or not isinstance(receipt, dict)
        or set(receipt) != {
            "action", "audit", "consumed", "issued_at", "payload",
            "receipt_sha256", "schema", "sequence", "ticket",
        }
        or receipt.get("schema")
        != "nysa.software-factory.operator-receipt/v1"
        or receipt.get("action") != "ready"
        or receipt.get("audit") != "no-authority"
        or receipt.get("consumed") is not False
        or receipt.get("payload") != {}
        or receipt.get("sequence") != sequence
        or receipt.get("ticket") != ticket
        or expected_receipt_sha256
        and receipt.get("receipt_sha256") != expected_receipt_sha256
        or not isinstance(receipt.get("issued_at"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("receipt_sha256", ""))
    ):
        raise DispatchError("pre-provider operator-ready state is invalid")


def reset_authorization(value: ResetAuthorization | str) -> ResetAuthorization:
    return value if isinstance(value, ResetAuthorization) else ResetAuthorization(value)


def validate_qualification_control_branch(
    product: Path,
    ticket: str,
    branch: str,
    main: str,
    remote_head: str,
    authorization: ResetAuthorization,
) -> None:
    ticket_path = f"factory/tickets/{ticket}.md"
    plan_path = f"factory/route-plans/{ticket}.json"
    base = git(product, "merge-base", main, remote_head).strip()
    if (
        base != authorization.source_product_sha
        or not git_succeeds(
            product, "merge-base", "--is-ancestor",
            authorization.source_product_sha, main,
        )
    ):
        raise DispatchError("qualification control reset source changed")
    try:
        source = json.loads(git(
            product, "show",
            f"{authorization.source_product_sha}:factory/QUALIFICATION.json",
        ))
    except json.JSONDecodeError as error:
        raise DispatchError("qualification control reset source is invalid") from error
    if (
        not isinstance(source, dict)
        or source.get("schema") != QUALIFICATION_SCHEMA_V2
        or source.get("factory_sha") != authorization.source_factory_sha
        or source.get("generation") != authorization.source_generation
        or not isinstance(source.get("tickets"), list)
        or ticket not in source["tickets"]
    ):
        raise DispatchError("qualification control reset source is invalid")
    changed = set(git(
        product, "diff", "--name-only", f"{base}..{remote_head}",
    ).splitlines())
    receipt_paths = [
        path for path in changed
        if re.fullmatch(
            rf"factory/receipts/{re.escape(ticket)}/ready-([1-9][0-9]*)[.]json",
            path,
        )
    ]
    expected_paths = {ticket_path, plan_path, *receipt_paths}
    if len(receipt_paths) > 1 or changed != expected_paths:
        raise DispatchError("qualification control reset contains product changes")
    commits = [
        tuple(line.split("\0"))
        for line in git(
            product, "log", "--first-parent", "--reverse",
            "--format=%H%x00%P%x00%an%x00%ae%x00%s",
            f"{base}..{remote_head}",
        ).splitlines()
    ]
    pins = [
        index for index, commit in enumerate(commits)
        if len(commit) == 5
        and commit[2:5] == (
            "Software Factory", "factory@local",
            f"{ticket}: pin kit and model route plan",
        )
    ]
    if len(pins) != 1:
        raise DispatchError("qualification control reset pin is invalid")
    pin_index = pins[0]
    pin = commits[pin_index]
    parents = pin[1].split()
    pin_paths = set(git(
        product, "diff-tree", "--no-commit-id", "--name-only", "-r", pin[0],
    ).splitlines())
    protected_pinned = pin_paths == {plan_path}
    if (
        len(parents) != 1
        or pin_paths not in ({ticket_path, plan_path}, {plan_path})
    ):
        raise DispatchError("qualification control reset pin is invalid")
    if receipt_paths:
        validate_operator_ready_lineage(
            product, ticket, branch, main, parents[0], ticket_path, plan_path,
        )
    elif parents[0] != authorization.source_product_sha:
        raise DispatchError("qualification control reset Ready source is invalid")
    before = git(product, "show", f"{parents[0]}:{ticket_path}")
    pinned = git(product, "show", f"{pin[0]}:{ticket_path}")
    try:
        route = json.loads(git(product, "show", f"{pin[0]}:{plan_path}"))
        receipt = (
            json.loads(git(product, "show", f"{pin[0]}:{receipt_paths[0]}"))
            if receipt_paths else None
        )
    except json.JSONDecodeError as error:
        raise DispatchError("qualification control reset evidence is invalid") from error
    previous = pin[0]
    for commit in commits[pin_index + 1:]:
        if (
            len(commit) != 5
            or commit[1].split() != [previous]
            or git(
                product, "diff-tree", "--no-commit-id", "--name-only",
                "-r", commit[0],
            ).splitlines() != [ticket_path]
        ):
            raise DispatchError("qualification control reset suffix is invalid")
        previous = commit[0]
    main_ticket = git(product, "show", f"{main}:{ticket_path}")
    remote_ticket = git(product, "show", f"{remote_head}:{ticket_path}")
    target_pin = git(product, "show", f"{main}:factory/KIT_PIN").strip()
    if (
        field(before, "State").casefold() != "ready"
        or field(pinned, "State").casefold() != "ready"
        or field(pinned, "Kit-SHA") != authorization.source_factory_sha
        or ticket_without_control(before) != ticket_without_control(pinned)
        or field(main_ticket, "State").casefold()
        != ("backlog" if receipt_paths else "ready")
        or (
            protected_pinned
            and (
                receipt_paths
                or before != pinned
                or field(before, "Kit-SHA")
                != authorization.source_factory_sha
                or not SHA.fullmatch(target_pin)
                or field(main_ticket, "Kit-SHA") != target_pin
            )
        )
        or (
            not protected_pinned
            and (field(before, "Kit-SHA") or field(main_ticket, "Kit-SHA"))
        )
        or (
            not receipt_paths
            and ticket_without_control(before) != ticket_without_control(main_ticket)
        )
        or field(remote_ticket, "State").casefold()
        not in {"ready", "planning", "building", "review", "blocked-escalated"}
        or field(remote_ticket, "Kit-SHA") != authorization.source_factory_sha
        or not isinstance(route, dict)
        or route.get("schema") != "ticket-model-route-plan/v1"
        or route.get("ticket") != ticket
        or route.get("kit_sha") != authorization.source_factory_sha
        or (
            receipt is not None
            and (
                not isinstance(receipt, dict)
                or receipt.get("schema")
                != "nysa.software-factory.operator-receipt/v1"
                or receipt.get("action") != "ready"
                or receipt.get("audit") != "no-authority"
                or receipt.get("ticket") != ticket
            )
        )
        or git_succeeds(product, "cat-file", "-e", f"{main}:{plan_path}")
        or (
            bool(receipt_paths)
            and git_succeeds(
                product, "cat-file", "-e", f"{main}:{receipt_paths[0]}"
            )
        )
    ):
        raise DispatchError("qualification control reset evidence is invalid")


def validate_preprovider_branch(
    product: Path,
    ticket: str,
    branch: str,
    main: str,
    reset: ResetAuthorization | str,
    remote_head: str,
    expected_ready_receipt_sha256: str = "",
    prior_authorization: ResetAuthorization | str | None = None,
) -> str:
    authorization = reset_authorization(reset)
    prior_authorization = prior_authorization or authorization.prior
    if remote_head != authorization.head:
        raise DispatchError("ticket remote branch does not match reset authorization")
    if authorization.source_factory_sha:
        validate_qualification_control_branch(
            product, ticket, branch, main, remote_head, authorization,
        )
        return remote_head
    base = git(product, "merge-base", main, remote_head).strip()
    if not SHA.fullmatch(base) or not git_succeeds(
        product, "merge-base", "--is-ancestor", base, main
    ):
        raise DispatchError("pre-provider branch lineage is invalid")
    ticket_path = f"factory/tickets/{ticket}.md"
    plan_path = f"factory/route-plans/{ticket}.json"
    changed = set(
        git(product, "diff", "--name-only", f"{base}..{remote_head}").splitlines()
    )
    receipt_paths = [
        path for path in changed
        if re.fullmatch(
            rf"factory/receipts/{re.escape(ticket)}/ready-([1-9][0-9]*)[.]json",
            path,
        )
    ]
    if len(receipt_paths) == 1 and changed == {ticket_path, receipt_paths[0]}:
        validate_operator_ready_lineage(
            product, ticket, branch, main, remote_head, ticket_path,
            plan_path, expected_ready_receipt_sha256, prior_authorization,
        )
        return remote_head
    if changed != {ticket_path, plan_path}:
        raise DispatchError("pre-provider branch is not control-only")
    commits = [
        tuple(line.split("\0"))
        for line in git(
            product,
            "log", "--first-parent",
            "--reverse", "--format=%H%x00%P%x00%an%x00%ae%x00%s",
            f"{base}..{remote_head}",
        ).splitlines()
    ]
    if not commits or any(
        len(item) != 5
        or item[2:4] != ("Software Factory", "factory@local")
        for item in commits
    ):
        raise DispatchError("pre-provider branch commits are not canonical")
    pin = f"{ticket}: pin kit and model route plan"
    materialize = f"{ticket}: materialize ticket state"
    transition = f"{ticket}: transition ticket state"
    supersede = f"{ticket}: supersede pre-provider control state"
    index = 0
    while index < len(commits):
        if commits[index][4] != pin:
            raise DispatchError("pre-provider branch commits are not canonical")
        index += 1
        if index < len(commits) and commits[index][4] == materialize:
            commit = commits[index]
            parents = commit[1].split()
            changed_paths = git(
                product, "diff-tree", "--no-commit-id", "--name-only",
                "-r", commit[0],
            ).splitlines()
            before = (
                git(product, "show", f"{parents[0]}:{ticket_path}")
                if len(parents) == 1 else ""
            )
            states = re.findall(r"^State:\s*(.*?)\s*$", before, re.I | re.M)
            expected = re.sub(
                r"^State:\s*Backlog\s*$", "State: Ready", before,
                count=1, flags=re.I | re.M,
            )
            if (
                len(parents) != 1
                or parents[0] != commits[index - 1][0]
                or changed_paths != [ticket_path]
                or len(states) != 1
                or states[0].lower() != "backlog"
                or git(product, "show", f"{commit[0]}:{ticket_path}") != expected
            ):
                raise DispatchError("pre-provider materialize commit is not canonical")
            index += 1
        if index < len(commits) and commits[index][4] == transition:
            index += 1
        if index == len(commits):
            break
        if index + 1 >= len(commits):
            raise DispatchError("pre-provider branch commits are not canonical")
        merge, reset = commits[index:index + 2]
        parents = merge[1].split()
        if (
            len(parents) != 2
            or parents[0] != commits[index - 1][0]
            or merge[4] != f"Merge commit '{parents[1]}' into {branch}"
            or not git_succeeds(
                product, "merge-base", "--is-ancestor", parents[1], main
            )
            or reset[1] != merge[0]
            or reset[4] != supersede
        ):
            raise DispatchError("pre-provider branch commits are not canonical")
        index += 2
    base_ticket = git(product, "show", f"{base}:{ticket_path}")
    main_ticket = git(product, "show", f"{main}:{ticket_path}")
    remote_ticket = git(product, "show", f"{remote_head}:{ticket_path}")
    route = json.loads(git(product, "show", f"{remote_head}:{plan_path}"))
    kit_sha = field(remote_ticket, "Kit-SHA")
    if (
        ticket_without_control(base_ticket) != ticket_without_control(main_ticket)
        or ticket_without_control(base_ticket) != ticket_without_control(remote_ticket)
        or field(main_ticket, "State").lower() != "ready"
        or field(main_ticket, "Kit-SHA")
        or field(remote_ticket, "State").lower() not in {"ready", "planning"}
        or not SHA.fullmatch(kit_sha)
        or not isinstance(route, dict)
        or route.get("schema") != "ticket-model-route-plan/v1"
        or route.get("ticket") != ticket
        or route.get("kit_sha") != kit_sha
        or git_succeeds(product, "cat-file", "-e", f"{main}:{plan_path}")
    ):
        raise DispatchError("pre-provider branch control state is invalid")
    return remote_head


def inspect_selected_preprovider_branches(
    product: Path,
    factory: Path,
    qualification_state: dict[str, Any],
    remote: str,
    *,
    exact_authorizations: bool = False,
    prepared_ready_receipts: dict[str, str] | None = None,
    protected_main: str = "",
    observed_heads: dict[str, str] | None = None,
) -> dict[str, str]:
    """Validate selected remote branches without changing them."""
    if qualification_state.get("schema") != QUALIFICATION_SCHEMA_V2:
        return {}
    prefix = ticket_branch_prefix(factory)
    if protected_main:
        main = protected_main
    else:
        git(product, "fetch", "--quiet", remote, "+main:refs/remotes/origin/main")
        main = git(product, "rev-parse", "refs/remotes/origin/main").strip()
    if not SHA.fullmatch(main):
        raise DispatchError("protected main is unavailable")
    authorizations = preprovider_reset_authorizations(
        factory, qualification_state, prefix,
    )
    prepared = prepared_ready_receipts or {}
    if (
        not isinstance(prepared, dict)
        or any(
            ticket not in qualification_state["tickets"]
            or not isinstance(digest, str)
            or not operator_receipt.DIGEST.fullmatch(digest)
            for ticket, digest in prepared.items()
        )
    ):
        raise DispatchError("prepared operator Ready receipts are invalid")
    divergent = {}
    prepared_divergent = set()
    if observed_heads is not None:
        expected = {prefix + ticket for ticket in qualification_state["tickets"]}
        if set(observed_heads) != expected:
            raise DispatchError("qualification remote branch observation is incomplete")
        refspecs = [
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
            for branch, head in observed_heads.items() if head
        ]
        if refspecs:
            git(product, "fetch", "--quiet", remote, *refspecs)
            if any(
                git(product, "rev-parse", f"refs/remotes/origin/{branch}").strip()
                != head for branch, head in observed_heads.items() if head
            ):
                raise DispatchError("qualification ticket remote branch changed during selection")
    for ticket in sorted(qualification_state["tickets"]):
        branch = prefix + ticket
        reference = f"refs/heads/{branch}"
        observed = (
            [observed_heads[branch], reference] if observed_heads[branch] else []
        ) if observed_heads is not None else git(
            product, "ls-remote", "--heads", "--", remote, reference,
        ).split()
        if not observed:
            if ticket in authorizations:
                raise DispatchError("authorized pre-provider branch is unavailable")
            continue
        if (
            len(observed) != 2
            or not SHA.fullmatch(observed[0])
            or observed[1] != reference
        ):
            raise DispatchError("ticket remote branch result is ambiguous")
        remote_head = observed[0]
        if observed_heads is None:
            git(
                product, "fetch", "--quiet", remote,
                f"+{reference}:refs/remotes/origin/{branch}",
            )
        if git_succeeds(product, "merge-base", "--is-ancestor", main, remote_head):
            continue
        authorized_head = authorizations.get(ticket, "")
        if authorized_head and remote_head == authorized_head.head:
            validate_preprovider_branch(
                product, ticket, branch, main, authorized_head, remote_head,
            )
            divergent[ticket] = remote_head
            continue
        if ticket in prepared:
            validate_preprovider_branch(
                product, ticket, branch, main, remote_head, remote_head,
                prepared[ticket], authorized_head or None,
            )
            divergent[ticket] = remote_head
            prepared_divergent.add(ticket)
            continue
        if not authorized_head:
            raise DispatchError(
                f"{ticket}: divergent remote branch lacks reset authorization"
            )
        validate_preprovider_branch(
            product, ticket, branch, main, authorized_head, remote_head,
        )
        divergent[ticket] = remote_head
    if exact_authorizations and (
        set(authorizations) - prepared_divergent
    ) != (set(divergent) - prepared_divergent):
        raise DispatchError("pre-provider reset authorization is not exact")
    return divergent


def selected_preprovider_reset_authorizations(
    product: Path,
    factory: Path,
    qualification_state: dict[str, Any] | None,
    remote: str,
    mapping_path: Path,
    state_dir: Path | None,
    protected_main: str = "",
    observed_heads: dict[str, str] | None = None,
) -> dict[str, ResetAuthorization]:
    prefix = ticket_branch_prefix(factory)
    prepared = (
        authenticated_prepared_ready_receipts(
            mapping_path, state_dir, qualification_state,
        )
        if (
            os.environ.get("FACTORY_KIT_TRUST_SCOPE")
            == "qualification-candidate"
            and os.environ.get("FACTORY_QUALIFICATION_MODE") == "isolated"
        )
        else {}
    )
    inspected = (
        inspect_selected_preprovider_branches(
            product, factory, qualification_state, remote,
            prepared_ready_receipts=prepared,
            protected_main=protected_main,
            observed_heads=observed_heads,
        )
        if qualification_state is not None
        else {}
    )
    resets = preprovider_reset_authorizations(
        factory, qualification_state, prefix,
    )
    for ticket in set(prepared) & set(inspected):
        if ticket not in resets or resets[ticket].head != inspected[ticket]:
            resets[ticket] = ResetAuthorization(
                inspected[ticket], prior=resets.get(ticket),
            )
    return resets


def reconcile_preprovider_branch(
    product: Path,
    worktree: Path,
    ticket: str,
    branch: str,
    remote: str,
    main: str,
    reset: ResetAuthorization | str,
) -> str:
    authorization = reset_authorization(reset)
    authorized_head = authorization.head
    remote_head = git(worktree, "rev-parse", "HEAD").strip()
    remote_head = validate_preprovider_branch(
        product, ticket, branch, main, authorization, remote_head,
    )
    ticket_path = f"factory/tickets/{ticket}.md"
    plan_path = f"factory/route-plans/{ticket}.json"
    base = git(product, "merge-base", main, remote_head).strip()
    receipt_paths = [
        path for path in git(
            product, "diff", "--name-only", f"{base}..{remote_head}",
        ).splitlines()
        if re.fullmatch(
            rf"factory/receipts/{re.escape(ticket)}/ready-[1-9][0-9]*[.]json",
            path,
        )
    ]
    git(
        worktree,
        "-c", "user.name=Software Factory",
        "-c", "user.email=factory@local",
        "merge", "--no-ff", "--no-edit", "-X", "theirs", main,
    )
    git(worktree, "checkout", main, "--", ticket_path)
    removed = [
        path for path in (plan_path, *receipt_paths)
        if git_succeeds(worktree, "cat-file", "-e", f"HEAD:{path}")
    ]
    if removed:
        git(worktree, "rm", "-f", "--", *removed)
    git(
        worktree,
        "-c", "user.name=Software Factory",
        "-c", "user.email=factory@local",
        "commit", "-m", f"{ticket}: supersede pre-provider control state",
        "--", ticket_path, *removed,
    )
    reset_head = git(worktree, "rev-parse", "HEAD").strip()
    git(
        worktree, "push",
        f"--force-with-lease=refs/heads/{branch}:{authorized_head}",
        "--", remote,
        f"{reset_head}:refs/heads/{branch}",
    )
    observed = git(
        worktree, "ls-remote", "--heads", "--", remote, f"refs/heads/{branch}"
    ).split()
    if not observed or observed[0] != reset_head:
        raise DispatchError("pre-provider branch reset remote verification failed")
    git(
        product, "fetch", "--quiet", remote,
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    )
    if git(worktree, "status", "--porcelain=v1", "-z"):
        raise DispatchError("pre-provider branch reset left a dirty worktree")
    return remote_head


def interrupted_reset_head(
    product: Path, ticket: str, branch: str, authorized_head: str,
    main: str, local: str, *, completed: bool = False,
) -> bool:
    def commit(value: str) -> tuple[str, ...]:
        return tuple(git(
            product, "show", "-s", "--format=%H%x00%P%x00%an%x00%ae%x00%s",
            value,
        ).strip().split("\0"))

    def merge(value: str) -> bool:
        item = commit(value)
        return (
            len(item) == 5
            and item[1].split() == [authorized_head, main]
            and item[2:4] == ("Software Factory", "factory@local")
            and item[4] == f"Merge commit '{main}' into {branch}"
        )

    item = commit(local)
    if merge(local):
        return not completed
    return (
        len(item) == 5
        and len(item[1].split()) == 1
        and merge(item[1])
        and item[2:5] == (
            "Software Factory", "factory@local",
            f"{ticket}: supersede pre-provider control state",
        )
        and git(product, "rev-parse", f"{local}^{{tree}}").strip()
        == git(product, "rev-parse", f"{main}^{{tree}}").strip()
    )


def interrupted_reset_dirty(
    product: Path, worktree: Path, ticket: str, authorized_head: str,
    main: str, local: str,
) -> bool:
    branch = ticket_branch_prefix(product / "factory") + ticket
    if not interrupted_reset_head(
        product, ticket, branch, authorized_head, main, local,
    ):
        return False
    base = git(product, "merge-base", main, authorized_head).strip()
    ticket_path = f"factory/tickets/{ticket}.md"
    allowed = {ticket_path, f"factory/route-plans/{ticket}.json"}
    allowed.update(
        path for path in git(
            product, "diff", "--name-only", f"{base}..{authorized_head}",
        ).splitlines()
        if re.fullmatch(
            rf"factory/receipts/{re.escape(ticket)}/ready-[1-9][0-9]*[.]json",
            path,
        )
    )
    changed = set(git(worktree, "diff", "HEAD", "--name-only").splitlines())
    changed.update(
        git(worktree, "diff", "--cached", "--name-only").splitlines()
    )
    main_blob = git(product, "rev-parse", f"{main}:{ticket_path}").strip()
    if (
        not changed
        or not changed <= allowed
        or git(worktree, "ls-files", "--others", "--exclude-standard")
        or ticket_path not in changed
        or git(worktree, "hash-object", ticket_path).strip() != main_blob
        or git(worktree, "rev-parse", f":{ticket_path}", check=False).strip()
        != main_blob
    ):
        return False
    for path in changed - {ticket_path}:
        if (worktree / path).exists() or git_succeeds(
            worktree, "cat-file", "-e", f":{path}",
        ):
            return False
    return True


def prepare_worktree(
    product: Path, worktree_root: Path, ticket: str, prefix: str, remote: str,
    authorized_reset: ResetAuthorization | str | None = None,
    protected_main: str = "",
    observed_remote_head: str | None = None,
    remote_prefetched: bool = False,
) -> tuple[Path, bool, bool, str]:
    authorization = (
        reset_authorization(authorized_reset) if authorized_reset else None
    )
    authorized_reset_head = authorization.head if authorization else ""
    branch = prefix + ticket
    safe_directory(worktree_root, "worktree root", owner_only=True)
    records = worktree_records(product)
    if protected_main:
        main = protected_main
    else:
        git(product, "fetch", "--quiet", remote, "+main:refs/remotes/origin/main")
        main = git(product, "rev-parse", "origin/main").strip()
    remote_branch = (
        [observed_remote_head, f"refs/heads/{branch}"]
        if observed_remote_head
        else []
    ) if observed_remote_head is not None else git(
        product, "ls-remote", "--heads", remote, f"refs/heads/{branch}"
    ).split()
    if remote_branch and (len(remote_branch) != 2 or remote_branch[1] != f"refs/heads/{branch}"):
        raise DispatchError("ticket remote branch result is ambiguous")
    matching = [item for item in records if item.get("branch") == f"refs/heads/{branch}"]
    if len(matching) > 1:
        raise DispatchError("ticket branch is checked out more than once")
    if matching:
        destination = Path(matching[0].get("worktree", "")).resolve(strict=True)
        if destination.parent != worktree_root or not re.fullmatch(
            r"cell-[1-6]", destination.name
        ):
            raise DispatchError("ticket branch is checked out outside a trusted cell")
        safe_directory(destination, "ticket worktree")
        local = git(destination, "rev-parse", "HEAD").strip()
        expected = remote_branch[0] if remote_branch else main
        dirty = bool(git(destination, "status", "--porcelain=v1", "-z"))
        if dirty:
            if (
                authorized_reset_head
                and expected == authorized_reset_head
                and interrupted_reset_dirty(
                    product, destination, ticket, expected, main, local,
                )
            ):
                git(destination, "reset", "--hard", expected)
                local = expected
            else:
                raise DispatchError("ticket worktree is dirty")
        if local != expected:
            if (
                authorized_reset_head
                and expected == authorized_reset_head
                and interrupted_reset_head(
                    product, ticket, branch, expected, main, local,
                )
            ):
                git(destination, "reset", "--hard", expected)
                local = expected
            else:
                raise DispatchError("ticket worktree branch is divergent or unpushed")
        reset_head = ""
        if remote_branch and not git_succeeds(
            product, "merge-base", "--is-ancestor", main, remote_branch[0]
        ):
            reset_head = reconcile_preprovider_branch(
                product, destination, ticket, branch, remote, main,
                authorization or "",
            )
        return destination, False, False, reset_head
    occupied = {
        Path(item.get("worktree", "")).resolve()
        for item in records
        if item.get("worktree")
    }
    destination = next(
        (
            worktree_root / f"cell-{number}"
            for number in range(1, 7)
            if worktree_root / f"cell-{number}" not in occupied
            and not (worktree_root / f"cell-{number}").exists()
            and not (worktree_root / f"cell-{number}").is_symlink()
        ),
        None,
    )
    if destination is None:
        raise DispatchError("no disposable execution cell is available")
    if git(product, "show-ref", "--verify", f"refs/heads/{branch}", check=False):
        branch_sha = git(product, "rev-parse", branch).strip()
        if not remote_branch or remote_branch[0] != branch_sha:
            raise DispatchError("existing ticket branch is divergent or unpushed")
        git(product, "worktree", "add", "--quiet", str(destination), branch)
        branch_created = False
    elif remote_branch:
        if not remote_prefetched:
            git(
                product, "fetch", "--quiet", remote,
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            )
        if git(product, "rev-parse", f"refs/remotes/origin/{branch}").strip() != remote_branch[0]:
            raise DispatchError("ticket remote branch changed during selection")
        git(
            product, "worktree", "add", "--quiet", "-b", branch,
            str(destination), f"origin/{branch}",
        )
        branch_created = True
    else:
        git(
            product,
            "worktree",
            "add",
            "--quiet",
            "-b",
            branch,
            str(destination),
            main,
        )
        branch_created = True
    reset_head = ""
    if remote_branch and not git_succeeds(
        product, "merge-base", "--is-ancestor", main, remote_branch[0]
    ):
        try:
            reset_head = reconcile_preprovider_branch(
                product, destination, ticket, branch, remote, main,
                authorization or "",
            )
        except (
            DispatchError, OSError, UnicodeError, json.JSONDecodeError,
            subprocess.SubprocessError,
        ):
            subprocess.run(
                ["git", "-C", str(product), "worktree", "remove", "--force",
                 str(destination)],
                capture_output=True,
                check=False,
            )
            if branch_created:
                subprocess.run(
                    ["git", "-C", str(product), "branch", "-D", branch],
                    capture_output=True,
                    check=False,
                )
            raise
    return destination, True, branch_created, reset_head


def reconcile_authorized_preprovider_branches(
    product: Path, worktree_root: Path, prefix: str, remote: str,
    authorizations: dict[str, ResetAuthorization | str], main: str,
) -> dict[str, tuple[str, str]]:
    reset = {}
    for ticket, raw_authorization in sorted(authorizations.items()):
        authorization = reset_authorization(raw_authorization)
        authorized_head = authorization.head
        branch = prefix + ticket
        observed = git(
            product, "ls-remote", "--heads", remote, f"refs/heads/{branch}"
        ).split()
        if len(observed) != 2 or observed[1] != f"refs/heads/{branch}":
            raise DispatchError("authorized pre-provider branch is unavailable")
        remote_head = observed[0]
        git(
            product, "fetch", "--quiet", remote,
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        )
        if git_succeeds(product, "merge-base", "--is-ancestor", main, remote_head):
            ticket_path = f"factory/tickets/{ticket}.md"
            if (
                field(git(product, "show", f"{main}:{ticket_path}"), "State").lower()
                == "backlog"
                and field(
                    git(product, "show", f"{remote_head}:{ticket_path}"), "State"
                ).lower() == "backlog"
                and interrupted_reset_head(
                    product, ticket, branch, authorized_head, main,
                    remote_head, completed=True,
                )
            ):
                reset[ticket] = (authorized_head, remote_head)
                release_reset_cell(
                    product, worktree_root, branch, remote_head,
                )
            continue
        if remote_head != authorized_head:
            raise DispatchError("ticket remote branch does not match reset authorization")
        destination, created, branch_created, reset_head = prepare_worktree(
            product, worktree_root, ticket, prefix, remote, authorization,
            main,
        )
        if reset_head != authorized_head:
            raise DispatchError("authorized pre-provider branch was not reset")
        reset[ticket] = (
            reset_head, git(destination, "rev-parse", "HEAD").strip(),
        )
        needs_materialization = field(
            git(product, "show", f"{main}:factory/tickets/{ticket}.md"), "State",
        ).casefold() == "backlog"
        if created or needs_materialization:
            if created:
                git(product, "worktree", "remove", "--force", str(destination))
            else:
                release_reset_cell(
                    product, worktree_root, branch, reset[ticket][1],
                )
            if branch_created:
                git(product, "branch", "-D", branch)
    return reset


def materialize_reset_backlog(
    product: Path,
    factory: Path,
    mapping_path: Path,
    remote: str,
    resets: dict[str, tuple[str, str]],
    main: str,
) -> None:
    state_dir = os.environ.get("FACTORY_CONTROLLER_STATE_DIR", "")
    for ticket, (_, expected_head) in sorted(resets.items()):
        ticket_path = f"factory/tickets/{ticket}.md"
        if field(git(product, "show", f"{main}:{ticket_path}"), "State").lower() != "backlog":
            continue
        branch = ticket_branch_prefix(factory) + ticket
        observed = git(
            product, "ls-remote", "--heads", "--", remote,
            f"refs/heads/{branch}",
        ).split()
        if len(observed) != 2 or observed[0] != expected_head:
            raise DispatchError("reset ticket branch is unavailable")
        git(
            product, "fetch", "--quiet", remote,
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        )
        current = field(
            git(product, "show", f"{observed[0]}:{ticket_path}"), "State"
        ).lower()
        if current == "ready":
            continue
        if current != "backlog" or not state_dir:
            raise DispatchError("reset ticket is not ready for materialization")
        result = subprocess.run(
            [
                sys.executable, str(Path(__file__).with_name("operator-cli.py")),
                "--product", str(product), "--state-dir", state_dir,
                "--expected-base-sha", expected_head,
                "ready", "--ticket", ticket,
            ],
            text=True, capture_output=True, check=False, timeout=300,
            env={**os.environ, "FACTORY_OPERATOR_MAP": str(mapping_path)},
        )
        if result.returncode:
            raise DispatchError(
                result.stderr.strip() or result.stdout.strip()
                or "reset ticket materialization failed"
            )


def create_lease(
    directory: Path, ticket: str, existing: set[str], ttl: int
) -> dict[str, Any]:
    lease = secrets.token_hex(32)
    while lease in existing:
        lease = secrets.token_hex(32)
    now = int(time.time())
    value = {
        "claimed_epoch": now,
        "expires_epoch": now + ttl,
        "lease_id": lease,
        "schema_version": 1,
        "ticket": ticket,
    }
    destination = directory / f"{ticket}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".lease-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(canonical(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
    return value


def lock(path: Path) -> None:
    for _ in range(100):
        try:
            path.mkdir(mode=0o700)
            return
        except FileExistsError:
            time.sleep(0.1)
    raise DispatchError("dispatcher lock is busy")


def admission_lock(path: Path) -> int:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise DispatchError("admission lock is unsafe")
        for _ in range(100):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                time.sleep(0.1)
        raise DispatchError("admission lock is busy")
    except Exception:
        os.close(descriptor)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-root", required=True, type=Path)
    parser.add_argument("--worktree-root", required=True, type=Path)
    parser.add_argument("--lease-ttl", type=int, default=900)
    parser.add_argument("--exclude-ticket", action="append", default=[])
    cohort = parser.add_mutually_exclusive_group()
    cohort.add_argument("--cohort", action="store_true")
    cohort.add_argument("--cohort-ack", default="")
    parser.add_argument("--cohort-limit", type=int, default=0)
    parser.add_argument("action", choices=("shadow", "claim"))
    args = parser.parse_args()
    launch_lock = args.factory_root / "factory" / ".launch.lock"
    lease_lock = args.factory_root / "factory" / ".dispatch-leases.lock"
    admission_descriptor = -1
    held_launch = held_lease = False
    created_worktree: Path | None = None
    created_branch = ""
    lease_created = False
    selected_ticket = ""
    preprovider_resets: dict[str, tuple[str, str]] = {}
    try:
        product = args.factory_root.resolve(strict=True)
        if any(not TICKET.fullmatch(item) for item in args.exclude_ticket):
            raise DispatchError("excluded ticket is invalid")
        if product != args.factory_root:
            raise DispatchError("factory root must be physical")
        safe_directory(product, "factory root")
        remote = certified_origin(product)
        factory = product / "factory"
        safe_directory(factory, "factory directory")
        if (factory / "KILL").exists() or (factory / "MAINTENANCE").exists():
            raise DispatchError("factory control blocks dispatch")
        if git(product, "status", "--porcelain=v1", "-z"):
            raise DispatchError("registered product checkout is dirty")
        git(product, "fetch", "--quiet", "origin", "+main:refs/remotes/origin/main")
        protected_main = git(
            product, "rev-parse", "refs/remotes/origin/main",
        ).strip()
        mapping_path = Path(os.environ.get(
            "FACTORY_OPERATOR_MAP", factory / "operator-map.json"
        ))
        if not mapping_path.is_absolute():
            raise DispatchError("operator map path is invalid")
        maximum = capacity(factory)
        qualification_state = qualification(product, factory, maximum)
        if (args.cohort or args.cohort_ack) and (
            args.action != "claim" or qualification_state is None
        ):
            raise DispatchError("cohort claim is available only in qualification")
        if (
            args.cohort_limit < 0
            or args.cohort_limit > 4
            or bool(args.cohort_limit) != bool(args.cohort)
        ):
            raise DispatchError("cohort claim limit is invalid")
        validate_qualification_ticket_sources(product, qualification_state)
        prefix = ticket_branch_prefix(factory)
        cohort_remote_heads = (
            remote_branch_heads(
                product, remote,
                [prefix + ticket for ticket in qualification_state["tickets"]],
            )
            if args.cohort and qualification_state is not None else None
        )
        controller_raw = os.environ.get("FACTORY_CONTROLLER_STATE_DIR", "")
        controller_state = Path(controller_raw) if controller_raw else None
        reset_authorizations = {} if args.cohort_ack else selected_preprovider_reset_authorizations(
            product, factory, qualification_state, remote, mapping_path,
            controller_state, protected_main if args.cohort else "",
            cohort_remote_heads,
        )
        reset_authorizations = {
            ticket: head for ticket, head in reset_authorizations.items()
            if readiness_executable(product, ticket)
        }
        if (
            args.action == "claim" and not args.cohort_ack
            and reset_authorizations
            and os.environ.get("FACTORY_KIT_TRUST_SCOPE") == "repository-test"
        ):
            raise DispatchError(
                "repository-test refuses pre-provider branch recovery"
            )
        if args.action == "claim" and not args.cohort_ack and reset_authorizations:
            safe_directory(args.worktree_root, "worktree root", owner_only=True)
            admission_descriptor = admission_lock(
                args.worktree_root / ".dispatch-admission.lock"
            )
            lock(launch_lock)
            held_launch = True
            reset_authorizations = selected_preprovider_reset_authorizations(
                product, factory, qualification_state, remote, mapping_path,
                controller_state, protected_main if args.cohort else "",
                cohort_remote_heads,
            )
            reset_authorizations = {
                ticket: head for ticket, head in reset_authorizations.items()
                if readiness_executable(product, ticket)
            }
            preprovider_resets = reconcile_authorized_preprovider_branches(
                product, args.worktree_root, prefix, remote,
                reset_authorizations, protected_main,
            )
            materialize_reset_backlog(
                product, factory, mapping_path, remote, preprovider_resets,
                protected_main,
            )
            if args.cohort:
                # Supported reset recovery intentionally advances one or more
                # observed refs. Freeze the post-recovery tuple before claim;
                # fresh cohorts retain their single observation above.
                cohort_remote_heads = remote_branch_heads(
                    product, remote,
                    [prefix + ticket for ticket in qualification_state["tickets"]],
                )
            launch_lock.rmdir()
            held_launch = False
        selected_tickets = (
            qualification_state["tickets"] if qualification_state else None
        )
        mapping = operator_mapping(mapping_path, selected_tickets)
        if args.cohort or args.cohort_ack:
            safe_directory(args.worktree_root, "worktree root", owner_only=True)
        if args.cohort_ack:
            admission_descriptor = admission_lock(
                args.worktree_root / ".dispatch-admission.lock"
            )
        transaction_path = (
            cohort_transaction_path(args.worktree_root, qualification_state)
            if qualification_state is not None and (args.cohort or args.cohort_ack)
            else None
        )
        transaction = (
            read_cohort_transaction(
                transaction_path, qualification_state, protected_main,
            )
            if transaction_path is not None else None
        )
        if transaction is not None:
            qualification_sha256 = hashlib.sha256(
                safe_file(
                    factory / "QUALIFICATION.json", "qualification manifest", 100_000,
                ).encode()
            ).hexdigest()
            operator_map_sha256 = hashlib.sha256(
                safe_file(mapping_path, "operator map").encode()
            ).hexdigest()
            if (
                transaction["qualification_sha256"] != qualification_sha256
                or transaction["operator_map_sha256"] != operator_map_sha256
                or transaction["protected_tree"]
                != git(product, "rev-parse", f"{protected_main}^{{tree}}").strip()
                or any(
                    item["ticket"] not in qualification_state["tickets"]
                    for item in transaction["tickets"]
                )
                or set(transaction["remote_heads"])
                != {prefix + item["ticket"] for item in transaction["tickets"]}
            ):
                raise DispatchError("qualification claim transaction drifted")
        if args.cohort_ack:
            if not re.fullmatch(r"[0-9a-f]{64}", args.cohort_ack):
                raise DispatchError("qualification claim acknowledgement is invalid")
            if transaction is not None:
                if transaction["transaction_sha256"] != args.cohort_ack:
                    raise DispatchError("qualification claim acknowledgement drifted")
                if controller_state is None:
                    raise DispatchError("qualification controller state is unavailable")
                claims_root = controller_state / "claims"
                safe_directory(claims_root, "controller claims", owner_only=True)
                for item in transaction["tickets"]:
                    claim = json.loads(safe_file(
                        claims_root / f"{item['ticket']}.json", "controller claim",
                    ))
                    lease = lease_record(factory / ".dispatch-leases", item["ticket"])
                    if (
                        lease is None
                        or claim.get("ticket") != item["ticket"]
                        or claim.get("lease") != lease["lease_id"]
                        or claim.get("branch") != prefix + item["ticket"]
                        or claim.get("worktree") != item.get("worktree")
                    ):
                        raise DispatchError("controller claim does not bind cohort transaction")
                transaction_path.unlink()
                directory = os.open(args.worktree_root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            print(canonical({
                "action": "ACK", "schema": SCHEMA, "status": "ACKNOWLEDGED",
                "transaction_sha256": args.cohort_ack,
            }))
            return
        if qualification_state is not None:
            maximum = qualification_state["capacity"]
            if qualification_state["done"] == qualification_state["target_done"]:
                print(canonical({
                    "action": "WAIT",
                    "qualification_done": qualification_state["done"],
                    "qualification_generation": qualification_state["generation"],
                    "qualification_target": qualification_state["target_done"],
                    "reason_code": "qualification_complete",
                    "schema": SCHEMA,
                    "status": "WAIT",
                }))
                return
        lease_dir = factory / ".dispatch-leases"
        if args.action == "claim":
            safe_directory(args.worktree_root, "worktree root", owner_only=True)
            if admission_descriptor < 0:
                admission_descriptor = admission_lock(
                    args.worktree_root / ".dispatch-admission.lock"
                )
        leased, lease_ids = lease_records(lease_dir)
        occupied = leased | active_tickets(factory)
        if transaction is None and len(occupied) >= maximum:
            print(canonical({
                "action": "WAIT", "reason_code": "capacity_full",
                "schema": SCHEMA, "status": "WAIT",
            }))
            return
        selected, refusals = candidates(
            factory,
            mapping, occupied | set(args.exclude_ticket),
            qualification_state,
        ) if transaction is None else (transaction["tickets"], [])
        refusal = {"admission_refusal": refusals[0]} if refusals else {}
        if not selected:
            print(canonical({
                "action": "WAIT", "reason_code": "no_candidate",
                "schema": SCHEMA, "status": "WAIT", **refusal,
            }))
            return
        ticket = selected[0]
        selected_ticket = ticket["ticket"]
        if args.action == "shadow":
            print(canonical({
                **ticket, "action": "SHADOW", "schema": SCHEMA,
                "status": "SHADOW", **refusal,
            }))
            return
        lock(launch_lock)
        held_launch = True
        lock(lease_lock)
        held_lease = True
        if (factory / "KILL").exists() or (factory / "MAINTENANCE").exists():
            raise DispatchError("factory control blocks dispatch")
        if git(product, "status", "--porcelain=v1", "-z"):
            raise DispatchError("registered product checkout changed during selection")
        lease_dir.mkdir(mode=0o700, exist_ok=True)
        safe_directory(lease_dir, "dispatcher lease directory")
        current_leased, current_lease_ids = lease_records(lease_dir)
        current_occupied = current_leased | active_tickets(factory)
        if args.cohort:
            if transaction is None:
                selected = selected[:min(
                    args.cohort_limit, maximum - len(current_occupied),
                )]
                if not selected:
                    raise DispatchError("qualification cohort has no claim capacity")
                branches = [prefix + item["ticket"] for item in selected]
                heads = {branch: cohort_remote_heads[branch] for branch in branches}
                transaction = {
                    "factory_sha": qualification_state["factory_sha"],
                    "generation": qualification_state["generation"],
                    "operator_map_sha256": hashlib.sha256(
                        safe_file(mapping_path, "operator map").encode()
                    ).hexdigest(),
                    "protected_main": protected_main,
                    "protected_tree": git(
                        product, "rev-parse", f"{protected_main}^{{tree}}",
                    ).strip(),
                    "qualification_sha256": hashlib.sha256(
                        safe_file(
                            factory / "QUALIFICATION.json", "qualification manifest", 100_000,
                        ).encode()
                    ).hexdigest(),
                    "remote_heads": heads,
                    "schema": COHORT_TRANSACTION_SCHEMA,
                    "tickets": selected,
                }
                transaction["transaction_sha256"] = cohort_transaction_digest(transaction)
                durable_write(transaction_path, transaction)
            results = []
            for item in transaction["tickets"]:
                ticket_name = item["ticket"]
                selected_ticket = ticket_name
                if ticket_name in active_tickets(factory) and ticket_name not in current_leased:
                    raise DispatchError("qualification cohort ticket became active without lease")
                destination, created, branch_created, reset_head = prepare_worktree(
                    product, args.worktree_root, ticket_name, prefix, remote,
                    reset_authorizations.get(ticket_name, ""), protected_main,
                    transaction["remote_heads"][prefix + ticket_name], True,
                )
                item["worktree"] = str(destination)
                if created:
                    created_worktree = destination
                if branch_created:
                    created_branch = prefix + ticket_name
                lease = lease_record(lease_dir, ticket_name)
                if lease is None:
                    if len(current_leased | active_tickets(factory)) >= maximum:
                        raise DispatchError("dispatcher capacity changed during cohort claim")
                    lease = create_lease(
                        lease_dir, ticket_name, lease_ids | current_lease_ids,
                        args.lease_ttl,
                    )
                    lease_ids.add(lease["lease_id"])
                    current_lease_ids.add(lease["lease_id"])
                    current_leased.add(ticket_name)
                lease_created = True
                reset_head = reset_head or preprovider_resets.get(ticket_name, ("", ""))[0]
                results.append({
                    **item, "action": "START", "branch": prefix + ticket_name,
                    "expires_epoch": lease["expires_epoch"],
                    "lease_id": lease["lease_id"],
                    "preprovider_reset_head": reset_head or None,
                    "schema": SCHEMA, "status": "CLAIMED", "worktree": str(destination),
                })
            # Persist the exact cell identities before publishing the response.
            transaction["transaction_sha256"] = cohort_transaction_digest(transaction)
            durable_write(transaction_path, transaction)
            print(canonical({
                "action": "START_BATCH", "claims": results, "schema": SCHEMA,
                "status": "CLAIMED",
                "transaction_sha256": transaction["transaction_sha256"],
            }))
            return
        if (
            len(current_occupied) >= maximum
            or ticket["ticket"] in current_leased
            or ticket["ticket"] in current_occupied
        ):
            raise DispatchError("dispatcher capacity changed during selection")
        destination, created, branch_created, reset_head = prepare_worktree(
            product, args.worktree_root, ticket["ticket"],
            prefix, remote, reset_authorizations.get(ticket["ticket"], ""),
            # Qualification freezes this exact product/main identity. Ordinary
            # dispatch re-fetches under its claim locks before branch creation.
            protected_main if qualification_state is not None else "",
        )
        reset_head = reset_head or preprovider_resets.get(
            ticket["ticket"], ("", ""),
        )[0]
        if created:
            created_worktree = destination
        if branch_created:
            created_branch = ticket_branch_prefix(factory) + ticket["ticket"]
        lease = create_lease(
            lease_dir,
            ticket["ticket"],
            lease_ids | current_lease_ids,
            args.lease_ttl,
        )
        lease_created = True
        print(
            canonical(
                {
                    **ticket,
                    "action": "START",
                    "branch": prefix + ticket["ticket"],
                    "expires_epoch": lease["expires_epoch"],
                    "lease_id": lease["lease_id"],
                    "preprovider_reset_head": reset_head or None,
                    "schema": SCHEMA,
                    "status": "CLAIMED",
                    "worktree": str(destination),
                    **refusal,
                }
            )
        )
    except (
        DispatchError,
        FileNotFoundError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        if created_worktree is not None and not lease_created:
            subprocess.run(
                ["git", "-C", str(args.factory_root), "worktree", "remove", "--force",
                 str(created_worktree)],
                capture_output=True,
                check=False,
            )
            if created_branch:
                subprocess.run(
                    ["git", "-C", str(args.factory_root), "branch", "-D", created_branch],
                    capture_output=True,
                    check=False,
                )
        failure = {
            "action": "ESCALATE", "error": str(error),
            "reason_code": "unsafe_state", "schema": SCHEMA, "status": "error",
        }
        if selected_ticket:
            failure["ticket"] = selected_ticket
        print(canonical(failure))
        raise SystemExit(2)
    finally:
        if held_lease:
            lease_lock.rmdir()
        if held_launch:
            launch_lock.rmdir()
        if admission_descriptor >= 0:
            os.close(admission_descriptor)


if __name__ == "__main__":
    main()
