#!/usr/bin/env python3
"""Create or reuse the exact ticket PR and gate review evidence on its checks."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from effective_ticket import ticket_branch_prefix  # noqa: E402
import operator_receipt  # noqa: E402
from external_transport import (  # noqa: E402
    temporarily_unavailable as github_temporarily_unavailable,
)
from approval_evidence import (  # noqa: E402
    ApprovalEvidenceError,
    trusted_approval_continuation_paths,
)
from legacy_approval_audit import (  # noqa: E402
    trusted_legacy_approval_audit_paths,
)
from narrator_evidence import (  # noqa: E402
    MAX_NARRATOR_EVIDENCE_BYTES,
    MAX_NARRATOR_EVIDENCE_FILES,
    PNG_END,
    PNG_SIGNATURE,
    authenticated_narrator_parent,
    trusted_narrator_evidence_paths,
)
from runtime_paths import canonical_factory_file  # noqa: E402
from refresh_semantics import (  # noqa: E402
    ClassificationError,
    preserved_control_paths,
    retained_control_paths,
)

SCHEMA = "nysa.software-factory.ticket-pr/v1"
REFRESH_RECEIPT_KEYS = {
    "schema", "ticket", "generation", "old_head", "base_head", "merge_head",
    "prior_reviewer_runs", "prior_approve_verdicts",
    "prior_request_changes_verdicts", "prior_narrator_runs",
    "prior_bundle_blob", "prior_approval_blob", "refreshed_at",
}
REFRESH_REVALIDATION_KEYS = {
    "revalidation_budget_micro_usd", "revalidation_factory_sha",
    "revalidation_generation",
}


class Refusal(ValueError):
    pass


class GitHubUnavailable(Refusal):
    pass


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command, cwd=cwd, text=True, capture_output=True, check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        if command[:1] == ["gh"] or "ls-remote" in command:
            raise GitHubUnavailable("GitHub request timed out") from error
        raise
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        if (
            command[:1] == ["gh"] or "ls-remote" in command
        ) and github_temporarily_unavailable(message):
            raise GitHubUnavailable(message)
        raise Refusal(message)
    return result


def git(root: Path, *arguments: str) -> str:
    command = ["git", "-C", str(root), *arguments]
    try:
        return run(command).stdout.strip()
    except Refusal:
        if arguments[0] != "ls-remote":
            raise
        return run(command).stdout.strip()


def project_repo(factory: Path) -> str:
    values = []
    for raw in (factory / "PROJECT.env").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(
            r"(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?",
            raw.strip(),
        )
        if match:
            values.append(match.group(1))
    if len(values) != 1:
        raise Refusal("GH_REPO is missing or ambiguous")
    return values[0]


def project_auto_merge_method(factory: Path) -> str:
    values = []
    for raw in (factory / "PROJECT.env").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(
            r"(?:export\s+)?AUTO_MERGE_METHOD\s*=\s*['\"]?(squash|merge|rebase)['\"]?",
            raw.strip(),
        )
        if match:
            values.append(match.group(1))
    if len(values) != 1:
        raise Refusal("AUTO_MERGE_METHOD is missing or ambiguous")
    return values[0]


def project_script(factory: Path, name: str) -> Path | None:
    values = []
    for raw in (factory / "PROJECT.env").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(
            rf"(?:export\s+)?{re.escape(name)}\s*=\s*([A-Za-z0-9._/-]+)",
            raw.strip(),
        )
        if match:
            values.append(match.group(1))
    if not values:
        return None
    if len(values) != 1:
        raise Refusal(f"{name} is ambiguous")
    relative = Path(values[0])
    if relative.is_absolute() or ".." in relative.parts:
        raise Refusal(f"{name} must be repository-contained")
    root = factory.parent.resolve(strict=True)
    candidate = root / relative
    info = candidate.lstat()
    resolved = candidate.resolve(strict=True)
    if (
        root not in resolved.parents
        or not stat.S_ISREG(info.st_mode)
        or candidate.is_symlink()
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or info.st_size > 1_000_000
        or not os.access(candidate, os.X_OK)
    ):
        raise Refusal(f"{name} is unsafe")
    return candidate


def project_nonvisual_paths(factory: Path) -> tuple[str, ...]:
    declarations = []
    for raw in (factory / "PROJECT.env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        match = re.fullmatch(r"(?:export\s+)?NONVISUAL_PATHS\s*=\s*(\S+)", line)
        if not re.match(r"(?:export\s+)?NONVISUAL_PATHS\s*=", line):
            continue
        if not match:
            raise Refusal("NONVISUAL_PATHS is invalid")
        value = match.group(1)
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise Refusal("NONVISUAL_PATHS is invalid")
            value = value[1:-1]
        if not re.fullmatch(r"[A-Za-z0-9._/-]+(?:,[A-Za-z0-9._/-]+)*", value):
            raise Refusal("NONVISUAL_PATHS is invalid")
        declarations.append(value)
    if not declarations:
        return ()
    if len(declarations) != 1:
        raise Refusal("NONVISUAL_PATHS is ambiguous")
    values = tuple(declarations[0].split(","))
    if (
        len(values) != len(set(values))
        or any(
            not value.endswith("/")
            or value.startswith("/")
            or "//" in value
            or any(part in {"", ".", ".."} for part in PurePosixPath(value).parts)
            for value in values
        )
        or any(
            left != right and right.startswith(left)
            for left in values for right in values
        )
    ):
        raise Refusal("NONVISUAL_PATHS is invalid")
    return tuple(sorted(values))


def project_preview_provider(factory: Path) -> str:
    values = re.findall(
        r"^(?:export\s+)?PREVIEW_PROVIDER\s*=\s*([^\s#]+)\s*$",
        (factory / "PROJECT.env").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if len(values) != 1 or values[0] not in {"none", "railway"}:
        raise Refusal("PREVIEW_PROVIDER must be exactly railway or none")
    return values[0]


def safe_repo_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 4096
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(part in {"", ".", ".."} for part in PurePosixPath(value).parts)
    ):
        raise Refusal("GitHub returned an unsafe PR path")
    return value


def pull_request_files(repo: str, number: int) -> list[dict[str, object]]:
    result = run([
        "gh", "api", "--paginate", f"repos/{repo}/pulls/{number}/files",
        "--jq", ".[] | {filename,status,previous_filename} | @base64",
    ])
    files = []
    for line in result.stdout.splitlines():
        try:
            value = json.loads(base64.b64decode(line, validate=True))
        except (ValueError, json.JSONDecodeError) as error:
            raise Refusal("GitHub returned malformed PR file evidence") from error
        if not isinstance(value, dict):
            raise Refusal("GitHub returned malformed PR file evidence")
        status = value.get("status")
        if status not in {"added", "modified", "removed", "renamed", "copied", "changed"}:
            raise Refusal("GitHub returned unknown PR file status")
        filename = safe_repo_path(value.get("filename"))
        previous = value.get("previous_filename")
        if status in {"renamed", "copied"}:
            previous = safe_repo_path(previous)
        elif previous is not None:
            raise Refusal("GitHub returned malformed PR file evidence")
        files.append({"filename": filename, "previous_filename": previous, "status": status})
    if not files or len(files) >= 3000:
        raise Refusal("GitHub returned empty or excessive PR file evidence")
    if len({item["filename"] for item in files}) != len(files):
        raise Refusal("GitHub returned duplicate PR file evidence")
    return files


def ticket_metadata_path(path: str, ticket: str) -> bool:
    exact = {
        f"factory/route-plans/{ticket}.json",
        f"factory/tickets/{ticket}.md",
        f"factory/tickets/{ticket}-bundle.md",
        f"factory/attestations/{ticket}/approval.json",
        f"factory/attestations/{ticket}/bundle.json",
        f"factory/attestations/{ticket}/refresh.json",
    }
    return path in exact or bool(re.fullmatch(
        rf"factory/tickets/{re.escape(ticket)}-evidence/"
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}[.]png",
        path,
    ))


def nonvisual_preview_evidence(
    factory: Path, repo: str, number: int, ticket: str, head: str,
) -> dict[str, object] | None:
    prefixes = project_nonvisual_paths(factory)
    if not prefixes:
        return None
    files = pull_request_files(repo, number)
    if any(item["status"] not in {"added", "modified"} for item in files):
        return None
    semantic = sorted(
        str(item["filename"])
        for item in files
        if not ticket_metadata_path(str(item["filename"]), ticket)
    )
    if (
        not semantic
        or any(path.startswith("factory/") for path in semantic)
        or any(not any(path.startswith(prefix) for prefix in prefixes) for path in semantic)
    ):
        return None
    digest = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "expected": head,
        "observed": [{"paths_sha256": digest, "policy": "nonvisual_paths"}],
        "reason": None,
        "status": "pass",
    }


def latest_reviewer_head(product: Path, workdir: Path, ticket: str) -> str:
    runs = product / "factory" / "runs"
    if not runs.is_dir() or runs.is_symlink():
        raise Refusal("reviewer run evidence is missing")
    reviewers = {}
    for path in sorted(runs.glob("*.meta")):
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise Refusal("reviewer run evidence is unsafe")
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in values:
                raise Refusal("reviewer run evidence is malformed")
            values[key] = value
        if (
            values.get("ticket") == ticket
            and values.get("role") == "reviewer"
            and values.get("phase") == "completed"
            and values.get("accounting_schema") == "1"
            and values.get("accounting_state") in {"completed", "abandoned_conservative"}
            and values.get("go_issued") == "1"
            and values.get("task_submitted") == "1"
            and values.get("exit_status") == "0"
            and values.get("role_exit") == "ok"
            and (
                values.get("accounting_state") != "abandoned_conservative"
                or values.get("cost_basis") == "conservative_reservation"
            )
        ):
            run_id = values.get("run_id", "")
            if not run_id or run_id in reviewers:
                raise Refusal("reviewer run evidence is ambiguous")
            reviewers[run_id] = values
    configured_ledger = os.environ.get("FACTORY_LEDGER", "")
    ledger = (
        Path(configured_ledger)
        if configured_ledger
        else canonical_factory_file(workdir, "runtime-ledger.csv")
    )
    if not ledger.is_file() or ledger.is_symlink():
        raise Refusal("reviewer ledger evidence is missing")
    with ledger.open(newline="", encoding="utf-8") as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if row.get("ticket") == ticket
            and row.get("role") == "reviewer"
            and row.get("exit_status") == "0"
        ]
    if not rows:
        raise Refusal("successful reviewer run evidence is missing")
    run_id = rows[-1].get("run_id", "")
    if run_id not in reviewers:
        raise Refusal("latest successful reviewer manifest is missing")
    reviewer = reviewers[run_id]
    if reviewer.get("cost_basis") != rows[-1].get("cost_basis"):
        raise Refusal("latest successful reviewer ledger evidence does not match")
    head = reviewer.get("role_head_before", "")
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise Refusal("reviewer head evidence is invalid")
    return head


def unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def preserved_refresh_metadata(
    workdir: Path, ticket: str, reviewed: str, head: str, changed: set[str],
) -> set[str]:
    if (
        os.environ.get("FACTORY_RELEASE_CONTRACT_VERSION") not in ("1.8.0", "2.0.0")
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            os.environ.get("FACTORY_TRANSITION_RECEIPT_SHA256", ""),
        )
    ):
        return set()
    relative = f"factory/attestations/{ticket}/refresh.json"
    path = workdir / relative
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        return set()
    try:
        receipt = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_json_object,
        )
        if not isinstance(receipt, dict):
            return set()
        counts = [
            receipt.get(name) for name in (
                "prior_reviewer_runs", "prior_approve_verdicts",
                "prior_request_changes_verdicts", "prior_narrator_runs",
            )
        ]
        if (
            (
                receipt.get("schema") == "nysa.software-factory.ticket-refresh/v1"
                and set(receipt) != REFRESH_RECEIPT_KEYS
                or receipt.get("schema")
                == "nysa.software-factory.ticket-refresh/v2"
                and set(receipt)
                != REFRESH_RECEIPT_KEYS | REFRESH_REVALIDATION_KEYS
                or receipt.get("schema") not in {
                    "nysa.software-factory.ticket-refresh/v1",
                    "nysa.software-factory.ticket-refresh/v2",
                }
            )
            or receipt.get("ticket") != ticket
            or isinstance(receipt.get("generation"), bool)
            or not isinstance(receipt.get("generation"), int)
            or receipt["generation"] < 1
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                   for value in counts)
            or counts[0] != counts[1] + counts[2]
            or any(
                not re.fullmatch(r"[0-9a-f]{40}", receipt.get(name, ""))
                for name in ("old_head", "base_head", "merge_head")
            )
            or any(
                value is not None
                and not re.fullmatch(r"[0-9a-f]{40}", value)
                for value in (
                    receipt.get("prior_bundle_blob"),
                    receipt.get("prior_approval_blob"),
                )
            )
            or not isinstance(receipt.get("refreshed_at"), str)
            or not receipt["refreshed_at"].endswith("Z")
            or receipt.get("schema")
            == "nysa.software-factory.ticket-refresh/v2"
            and (
                receipt.get("revalidation_budget_micro_usd") != 20_000_000
                or not re.fullmatch(
                    r"[0-9a-f]{40}",
                    receipt.get("revalidation_factory_sha", ""),
                )
                or isinstance(receipt.get("revalidation_generation"), bool)
                or not isinstance(receipt.get("revalidation_generation"), int)
                or not 1 <= receipt["revalidation_generation"] <= receipt["generation"]
            )
        ):
            return set()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()
    receipt_commit = git(workdir, "log", "-1", "--format=%H", head, "--", relative)
    if (
        git(workdir, "rev-list", "--parents", "-n", "1", receipt_commit).split()
        != [receipt_commit, receipt["merge_head"]]
        or git(
            workdir, "rev-list", "--parents", "-n", "1", receipt["merge_head"],
        ).split()
        != [
            receipt["merge_head"], receipt["old_head"], receipt["base_head"],
        ]
    ):
        return set()
    for ancestor, descendant in (
        (reviewed, receipt["old_head"]),
        (receipt_commit, head),
    ):
        if subprocess.run(
            ["git", "-C", str(workdir), "merge-base", "--is-ancestor",
             ancestor, descendant],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode:
            return set()
    try:
        preserved = preserved_control_paths(
            workdir, receipt["old_head"], receipt["base_head"],
        )
    except ClassificationError:
        return set()
    if preserved is None:
        return set()
    return {
        relative,
        *retained_control_paths(workdir, head, receipt["base_head"], changed),
    }


def validate_review_lineage(product: Path, workdir: Path, ticket: str, head: str) -> None:
    reviewed = latest_reviewer_head(product, workdir, ticket)
    run(["git", "-C", str(workdir), "merge-base", "--is-ancestor", reviewed, head])
    changed = set(git(workdir, "diff", "--name-only", f"{reviewed}..{head}").splitlines())
    ticket_text = git(workdir, "show", f"{head}:factory/tickets/{ticket}.md")
    kit_shas = re.findall(r"^Kit-SHA:\s*([0-9a-f]{40})\s*$", ticket_text, re.MULTILINE)
    kit_sha = kit_shas[0] if len(kit_shas) == 1 else ""
    route_path = f"factory/route-plans/{ticket}.json"
    trusted_metadata = {
        route_path,
        f"factory/attestations/{ticket}/bundle.json",
        f"factory/tickets/{ticket}-bundle.md",
        f"factory/tickets/{ticket}.md",
    }
    refresh_metadata = preserved_refresh_metadata(
        workdir, ticket, reviewed, head, changed,
    )
    trusted_metadata.update(refresh_metadata)
    trusted_metadata.update(
        trusted_narrator_evidence_paths(
            workdir, ticket, reviewed, head, changed,
            authenticated_narrator_parent(
                Path(os.environ.get("FACTORY_CONTROLLER_STATE_DIR", "")),
                ticket,
                os.environ.get("FACTORY_PROJECT", ""),
                ticket_branch_prefix(product / "factory") + ticket,
                kit_sha,
                os.environ.get("FACTORY_RELEASE_CONTRACT_VERSION", ""),
                head,
            ),
        )
    )
    trusted_metadata.update(
        trusted_legacy_approval_audit_paths(
            workdir, ticket, head, changed, refresh_metadata,
        )
    )
    approval_path = f"factory/attestations/{ticket}/approval.json"
    if approval_path in changed:
        ticket_text = git(workdir, "show", f"{head}:factory/tickets/{ticket}.md")
        kit_shas = re.findall(r"^Kit-SHA:\s*([0-9a-f]{40})\s*$", ticket_text, re.MULTILINE)
        if len(kit_shas) != 1:
            raise Refusal("approval continuation Kit-SHA is missing or ambiguous")
        continuation_reviewed = reviewed
        if refresh_metadata:
            try:
                continuation_reviewed = json.loads(
                    git(workdir, "show", f"{head}:{approval_path}")
                ).get("reviewed_sha", "")
            except (AttributeError, json.JSONDecodeError):
                continuation_reviewed = ""
        try:
            trusted_metadata.update(trusted_approval_continuation_paths(
                workdir,
                ticket,
                project_repo(product / "factory"),
                ticket_branch_prefix(product / "factory") + ticket,
                kit_shas[0],
                project_auto_merge_method(product / "factory"),
                continuation_reviewed,
                head,
                changed,
            ))
        except ApprovalEvidenceError as error:
            raise Refusal(str(error)) from error
    pin_path = "factory/KIT_PIN"
    untrusted = changed - trusted_metadata
    if untrusted - {pin_path} or pin_path in untrusted and route_path not in changed:
        raise Refusal("ticket implementation changed after the latest successful review")
    if route_path not in changed:
        return

    state_root = os.environ.get("FACTORY_MODEL_STATE_ROOT", "")
    project = os.environ.get("FACTORY_PROJECT", "")
    ticket_text = (workdir / f"factory/tickets/{ticket}.md").read_text(encoding="utf-8")
    kit_shas = re.findall(r"^Kit-SHA:\s*([0-9a-f]{40})\s*$", ticket_text, re.MULTILINE)
    if not Path(state_root).is_absolute() or not project or len(kit_shas) != 1:
        raise Refusal("route migration validation environment is missing")
    release_sha = kit_shas[0]
    manager = Path(__file__).resolve().parent / "model-manager.py"
    route_file = workdir / route_path
    command = [
        sys.executable, "-B", str(manager), "select",
        "--state-root", state_root,
        "--project", project,
        "--ticket-plan", str(route_file),
        "--ticket", ticket,
        "--kit-sha", release_sha,
        "--role", "narrator",
    ]
    policy = os.environ.get("FACTORY_MODEL_POLICY_FILE", "")
    if policy:
        command.extend(["--policy-file", policy])
    run(command)

    prior_blob = run([
        "git", "-C", str(workdir), "show", f"{reviewed}:{route_path}",
    ]).stdout.encode("utf-8")
    try:
        prior = json.loads(prior_blob)
        current = json.loads(route_file.read_text(encoding="utf-8"))
        revisions = current["revisions"]
        if prior.get("schema") == "ticket-model-route-journal/v2":
            prefix = prior["revisions"]
            suffix = revisions[len(prefix):]
            valid_lineage = revisions[:len(prefix)] == prefix
        elif prior.get("schema") == "ticket-model-route-plan/v1":
            prefix = []
            suffix = revisions[1:]
            valid_lineage = base64.b64decode(
                revisions[0]["body"]["legacy_plan_b64"], validate=True
            ) == prior_blob
        else:
            valid_lineage = False
            suffix = []
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        valid_lineage = False
        suffix = []
    if (
        not valid_lineage
        or not suffix
        or any(item.get("body", {}).get("kind") != "release-migration" for item in suffix)
    ):
        raise Refusal("post-review route migration lineage is invalid")
    if pin_path in untrusted and (
        not re.fullmatch(
            rf"100644 blob [0-9a-f]{{40}}\t{re.escape(pin_path)}\n?",
            run([
                "git", "-C", str(workdir), "ls-tree", head, "--", pin_path,
            ]).stdout,
        )
        or run([
            "git", "-C", str(workdir), "show", f"{head}:{pin_path}",
        ]).stdout != release_sha + "\n"
    ):
        raise Refusal("post-review route migration Kit-SHA is invalid")


def required_check_names(repo: str) -> set[str]:
    try:
        pages = json.loads(run([
            "gh", "api", f"repos/{repo}/rules/branches/main?per_page=100",
            "--paginate", "--slurp",
        ]).stdout)
    except json.JSONDecodeError as error:
        raise Refusal("GitHub returned invalid branch-rule evidence") from error
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise Refusal("GitHub returned invalid branch-rule evidence")
    rules = [rule for page in pages for rule in page]
    required = {}
    for rule in rules:
        if not isinstance(rule, dict):
            raise Refusal("GitHub returned malformed branch-rule evidence")
        if rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            raise Refusal("GitHub returned malformed required-check rules")
        checks = parameters.get("required_status_checks")
        if not isinstance(checks, list):
            raise Refusal("GitHub returned malformed required-check rules")
        for item in checks:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("context"), str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9 ._:/()=-]{0,199}", item["context"]
                )
                or not isinstance(item.get("integration_id"), int)
                or item["integration_id"] <= 0
            ):
                raise Refusal("GitHub required check is not app-bound")
            prior = required.setdefault(item["context"], item["integration_id"])
            if prior != item["integration_id"]:
                raise Refusal("GitHub required-check identity is ambiguous")
    if not required:
        raise Refusal("main has no app-bound required GitHub checks")
    return set(required)


def required_check_status(repo: str, number: int) -> tuple[str, list[str]]:
    required = required_check_names(repo)
    result = subprocess.run(
        [
            "gh", "pr", "checks", str(number), "--repo", repo, "--required",
            "--json", "name,state,bucket",
        ],
        text=True, capture_output=True, check=False,
    )
    message = result.stderr.strip() or result.stdout.strip()
    if (
        result.returncode
        and not result.stdout.strip()
        and github_temporarily_unavailable(message)
    ):
        raise GitHubUnavailable(message)
    if result.returncode not in (0, 1, 8):
        raise Refusal(message or "GitHub required-check query failed")
    if (
        result.returncode == 1
        and not result.stdout.strip()
        and re.fullmatch(
            r"no (?:required )?checks reported on the '[^'\r\n]+' branch",
            result.stderr.strip(),
        )
    ):
        return "wait", ["required checks not reported"]
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise Refusal("GitHub returned invalid required-check evidence") from error
    if not isinstance(checks, list):
        raise Refusal("GitHub returned invalid required-check evidence")
    if not checks:
        return "wait", ["required checks not reported"]
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9 ._:/()=-]{0,199}", item["name"]
        )
        for item in checks
    ):
        raise Refusal("GitHub returned malformed required-check evidence")
    names = [item["name"] for item in checks]
    if len(names) != len(set(names)) or set(names) - required:
        raise Refusal("GitHub required-check evidence is ambiguous")
    missing = sorted(required - set(names))
    if missing:
        return "wait", [f"required check not reported: {name}" for name in missing]
    buckets = {item.get("bucket") for item in checks if isinstance(item, dict)}
    if len(buckets) == 0 or not buckets <= {"pass", "fail", "pending", "skipping", "cancel"}:
        raise Refusal("GitHub returned unknown required-check state")
    if buckets & {"pending"}:
        return "wait", sorted(str(item.get("name")) for item in checks if item.get("bucket") == "pending")
    if buckets & {"fail", "skipping", "cancel"}:
        return "failed", sorted(
            str(item.get("name")) for item in checks
            if item.get("bucket") in {"fail", "skipping", "cancel"}
        )
    return "pass", []


def railway_preview_evidence(
    repo: str, number: int, head: str,
) -> tuple[list[str], dict[str, object]]:
    result = run([
        "gh", "pr", "view", str(number), "--repo", repo,
        "--json", "comments",
    ])
    try:
        value = json.loads(result.stdout)
        comments = value["comments"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise Refusal("GitHub returned invalid preview evidence") from error
    if not isinstance(comments, list):
        raise Refusal("GitHub returned invalid preview evidence")
    bodies = []
    for comment in comments:
        if not isinstance(comment, dict):
            raise Refusal("GitHub returned malformed preview evidence")
        author = comment.get("author")
        body = comment.get("body")
        if not (
            isinstance(author, dict)
            and author.get("login") == "railway-app"
            and isinstance(body, str)
        ):
            continue
        if "railway-bot-comment-version=2" in body:
            bodies.append(body)
    if not bodies:
        return [], {
            "expected": head, "observed": [], "reason": "not_reported",
            "status": "wait",
        }
    body = bodies[-1]
    rows = re.findall(
        r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*"
        r"\[Web\]\((https://[^\s()]+)\)\s*\|[^\n]*$",
        body,
        re.M,
    )
    if not rows:
        raise Refusal("Railway preview comment has no service evidence")
    urls = []
    deployments = []
    services = []
    for service, status_cell, candidate in rows:
        service_name = service.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}", service_name):
            raise Refusal("Railway preview service name is malformed")
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".up.railway.app")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise Refusal("Railway preview URL is malformed")
        urls.append(candidate.rstrip("/"))
        links = re.findall(r"\[View Logs\]\((https://[^\s()]+)\)", status_cell)
        if len(links) != 1:
            raise Refusal("Railway preview deployment link is malformed")
        detail = urlsplit(links[0])
        match = re.fullmatch(
            r"/project/([0-9a-f-]{36})/service/([0-9a-f-]{36})", detail.path
        )
        query = parse_qs(detail.query, strict_parsing=True)
        if (
            detail.scheme != "https"
            or detail.hostname != "railway.com"
            or not match
            or set(query) != {"id", "environmentId"}
            or any(len(values) != 1 for values in query.values())
            or not all(
                re.fullmatch(r"[0-9a-f-]{36}", query[name][0])
                for name in ("id", "environmentId")
            )
        ):
            raise Refusal("Railway preview deployment link is malformed")
        deployment_id = query["id"][0]
        services.append({
            "deployment_id": deployment_id,
            "service": service_name,
            "log_url": links[0],
            "url": candidate.rstrip("/"),
            "hostname": parsed.hostname,
        })
    if (
        len({item["service"] for item in services}) != len(services)
        or len({item["log_url"] for item in services}) != len(services)
        or len({item["url"] for item in services}) != len(services)
    ):
        raise Refusal("Railway preview service evidence is ambiguous")
    try:
        deployment_result = run([
            "gh", "api", f"repos/{repo}/deployments?sha={head}&per_page=100",
        ])
        deployment_values = json.loads(deployment_result.stdout)
    except Refusal:
        return urls, {
            "expected": head, "observed": [], "reason": "github_unavailable",
            "status": "wait",
        }
    except json.JSONDecodeError as error:
        raise Refusal("GitHub returned invalid deployment evidence") from error
    if (
        not isinstance(deployment_values, list)
        or len(deployment_values) > 100
        or any(not isinstance(item, dict) for item in deployment_values)
    ):
        raise Refusal("GitHub returned invalid deployment evidence")
    ids = [item.get("id") for item in deployment_values]
    if any(not isinstance(value, int) or value <= 0 for value in ids) or len(ids) != len(set(ids)):
        raise Refusal("GitHub returned ambiguous deployment evidence")
    exact_deployments = [
        item for item in deployment_values
        if item.get("sha") == head and item.get("ref") == head
    ]
    if not exact_deployments:
        return urls, {
            "expected": head, "observed": [], "reason": "stale_or_pending",
            "status": "wait",
        }
    deployment = max(exact_deployments, key=lambda item: item["id"])
    creator = deployment.get("creator")
    login = creator.get("login") if isinstance(creator, dict) else None
    provider = login.removesuffix("[bot]") if isinstance(login, str) else None
    environment_name = deployment.get("environment")
    if (
        deployment.get("task") != "deploy"
        or deployment.get("transient_environment") is not True
        or deployment.get("production_environment") is not False
        or provider != "railway-app"
        or not isinstance(environment_name, str)
        or not environment_name
    ):
        raise Refusal("GitHub deployment identity is invalid")
    try:
        status_result = run([
            "gh", "api",
            f"repos/{repo}/deployments/{deployment['id']}/statuses?per_page=100",
        ])
        status_values = json.loads(status_result.stdout)
        commit_result = run(["gh", "api", f"repos/{repo}/commits/{head}/status"])
        commit_value = json.loads(commit_result.stdout)
        detailed_result = run([
            "gh", "api", f"repos/{repo}/commits/{head}/statuses?per_page=100",
        ])
        detailed_statuses = json.loads(detailed_result.stdout)
    except Refusal:
        return urls, {
            "expected": head, "observed": [], "reason": "github_unavailable",
            "status": "wait",
        }
    except json.JSONDecodeError as error:
        raise Refusal("GitHub returned invalid preview status evidence") from error
    if (
        not isinstance(status_values, list)
        or len(status_values) > 100
        or any(not isinstance(item, dict) for item in status_values)
    ):
        raise Refusal("GitHub returned invalid deployment status evidence")
    if not status_values:
        return urls, {
            "expected": head, "observed": [], "reason": "stale_or_pending",
            "status": "wait",
        }
    status_ids = [item.get("id") for item in status_values]
    if (
        any(not isinstance(value, int) or value <= 0 for value in status_ids)
        or len(status_ids) != len(set(status_ids))
    ):
        raise Refusal("GitHub returned ambiguous deployment status evidence")
    deployment_status = max(status_values, key=lambda item: item["id"])
    status_creator = deployment_status.get("creator")
    status_login = status_creator.get("login") if isinstance(status_creator, dict) else None
    status_provider = status_login.removesuffix("[bot]") if isinstance(status_login, str) else None
    if (
        deployment_status.get("environment") != environment_name
        or status_provider != provider
    ):
        raise Refusal("GitHub deployment status identity is invalid")
    if deployment_status.get("state") != "success":
        return urls, {
            "expected": head, "observed": [], "reason": "stale_or_pending",
            "status": "wait",
        }
    repository = commit_value.get("repository") if isinstance(commit_value, dict) else None
    statuses = commit_value.get("statuses") if isinstance(commit_value, dict) else None
    if (
        not isinstance(commit_value, dict)
        or commit_value.get("sha") != head
        or not isinstance(repository, dict)
        or repository.get("full_name") != repo
        or not isinstance(statuses, list)
        or len(statuses) > 100
        or any(not isinstance(item, dict) for item in statuses)
        or not isinstance(detailed_statuses, list)
        or len(detailed_statuses) > 100
        or any(not isinstance(item, dict) for item in detailed_statuses)
    ):
        raise Refusal("GitHub returned invalid commit status evidence")
    status_ids = [item.get("id") for item in statuses]
    detailed_ids = [item.get("id") for item in detailed_statuses]
    if (
        any(not isinstance(value, int) or value <= 0 for value in status_ids)
        or len(status_ids) != len(set(status_ids))
        or any(not isinstance(value, int) or value <= 0 for value in detailed_ids)
        or len(detailed_ids) != len(set(detailed_ids))
    ):
        raise Refusal("GitHub returned ambiguous commit status evidence")
    for service in services:
        matching = [item for item in statuses if item.get("target_url") == service["log_url"]]
        if len(matching) != 1:
            if not matching:
                return urls, {
                    "expected": head, "observed": deployments,
                    "reason": "stale_or_pending", "status": "wait",
                }
            raise Refusal("GitHub preview service status is ambiguous")
        item = matching[0]
        detailed = [value for value in detailed_statuses if value.get("id") == item["id"]]
        if len(detailed) != 1:
            raise Refusal("GitHub preview service status identity is invalid")
        service_creator = detailed[0].get("creator")
        service_login = (
            service_creator.get("login")
            if isinstance(service_creator, dict) else None
        )
        service_provider = (
            service_login.removesuffix("[bot]")
            if isinstance(service_login, str) else None
        )
        if (
            service_provider != provider
            or any(
                detailed[0].get(key) != item.get(key)
                for key in ("description", "state", "target_url")
            )
        ):
            raise Refusal("GitHub preview service status identity is invalid")
        if item.get("state") != "success":
            return urls, {
                "expected": head, "observed": deployments,
                "reason": "stale_or_pending", "status": "wait",
            }
        if item.get("description") != f"Success - {service['hostname']}":
            raise Refusal("GitHub preview service hostname is invalid")
        deployments.append({
            "deployment_id": service["deployment_id"],
            "service": service["service"],
            "sha": head,
            "status": "SUCCESS",
            "url": service["url"],
        })
    return list(dict.fromkeys(urls)), {
        "expected": head,
        "observed": deployments,
        "reason": None,
        "status": "pass",
    }


def preview_preflight(
    factory: Path, ticket: str, head: str, identity: dict[str, object],
) -> dict[str, object] | None:
    script = project_script(factory, "PREVIEW_PREFLIGHT_SCRIPT")
    if script is None:
        return None
    payload = {
        "head": head,
        "previews": identity.get("observed"),
        "schema": "nysa.software-factory.preview-preflight-input/v1",
        "ticket": ticket,
    }
    result = subprocess.run(
        [str(script)], input=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        text=True, capture_output=True, check=False, timeout=120,
        env={
            "HOME": os.environ.get("HOME", ""),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    if result.returncode or len(result.stdout.encode()) > 1_000_000:
        raise Refusal("preview preflight failed")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise Refusal("preview preflight returned invalid evidence") from error
    status = value.get("status") if isinstance(value, dict) else None
    reason = value.get("reason") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"evidence", "head", "reason", "schema", "status"}
        or value.get("schema")
        != "nysa.software-factory.preview-preflight/v1"
        or value.get("head") != head
        or status not in {"pass", "wait", "fail"}
        or not isinstance(value.get("evidence"), dict)
        or (
            status == "pass" and reason is not None
        )
        or (
            status != "pass"
            and (
                not isinstance(reason, str)
                or not re.fullmatch(r"[a-z0-9_.-]{1,128}", reason)
            )
        )
    ):
        raise Refusal("preview preflight returned invalid evidence")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"T-[0-9]+", args.ticket):
            raise Refusal("invalid ticket identifier")
        product = Path(os.environ["FACTORY_ROOT"]).resolve(strict=True)
        workdir = args.workdir.resolve(strict=True)
        factory = product / "factory"
        preview_provider = project_preview_provider(factory)
        branch = ticket_branch_prefix(factory) + args.ticket
        expected_origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
        origins = git(product, "remote", "get-url", "--push", "--all", "origin").splitlines()
        if not expected_origin or origins != [expected_origin]:
            raise Refusal("product origin does not match certification")
        if git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD") != branch:
            raise Refusal("ticket worktree is on the wrong branch")
        if git(workdir, "status", "--porcelain", "--untracked-files=all"):
            raise Refusal("ticket worktree is dirty")
        head = git(workdir, "rev-parse", "HEAD")
        remote = run(
            ["git", "ls-remote", "--heads", expected_origin, f"refs/heads/{branch}"]
        ).stdout.split()
        if not remote or remote[0] != head:
            raise Refusal("ticket branch is not pushed at the exact local head")
        lease_id = os.environ.get("FACTORY_DISPATCH_LEASE_ID", "")
        contract = os.environ.get("FACTORY_RELEASE_CONTRACT_VERSION", "")
        if lease_id and not re.fullmatch(r"[0-9a-f]{64}", lease_id):
            raise Refusal("dispatcher lease is invalid")
        if contract in ("1.8.0", "2.0.0"):
            stage = os.environ.get("FACTORY_TRANSITION_STAGE", "")
            if not re.fullmatch(
                r"(?:RUN (?:reviewer|narrator)|AWAIT-(?:OPERATOR|MERGE) .+)",
                stage,
            ):
                raise Refusal("transition receipt does not authorize ticket PR verification")
        else:
            stage_command = [
                "bash", str(Path(__file__).resolve().parent / "next-stage.sh"),
                "--ticket", args.ticket,
            ]
            if lease_id:
                stage_command.extend(["--lease", lease_id])
            stage_command.extend(["--workdir", str(workdir)])
            stage = run(stage_command).stdout.strip()
        if stage.startswith("RUN reviewer"):
            boundary = "reviewer"
        elif stage.startswith("RUN narrator"):
            boundary = "narrator"
        elif stage.startswith(("AWAIT-OPERATOR", "AWAIT-MERGE")):
            boundary = "publication"
        else:
            raise Refusal("ticket PR verification requires the reviewer or narrator stage")
        if boundary in {"narrator", "publication"}:
            validate_review_lineage(product, workdir, args.ticket, head)
        repo = project_repo(factory)
        fields = "number,headRefName,baseRefName,headRefOid,url,state"
        pr: dict = {}

        def candidates() -> list[dict]:
            value = json.loads(run([
                "gh", "pr", "list", "--repo", repo, "--state", "open",
                "--head", branch, "--base", "main", "--json", fields,
            ]).stdout)
            if not isinstance(value, list):
                raise Refusal("GitHub returned invalid ticket PR evidence")
            return value

        prs = candidates()
        if not prs:
            run([
                "gh", "pr", "create", "--repo", repo, "--head", branch,
                "--base", "main", "--title", f"{args.ticket}: implementation",
                "--body", "Factory ticket implementation. Approval and merge remain protected.",
            ])
            prs = candidates()
        if len(prs) != 1:
            raise Refusal("expected exactly one PR for the exact ticket branch")
        pr = prs[0]
        if (
            pr.get("headRefName") != branch
            or pr.get("baseRefName") != "main"
            or pr.get("headRefOid") != head
            or pr.get("state") != "OPEN"
            or not isinstance(pr.get("number"), int)
            or pr["number"] <= 0
        ):
            raise Refusal("ticket PR branch, base, head, or state is invalid")
        check_status, checks = required_check_status(repo, pr["number"])
        preview_urls = []
        preview_identity: dict[str, object] | None = None
        preview_gate: dict[str, object] | None = None
        publication_mode: str | None = None
        if (
            boundary in {"narrator", "publication"}
            and check_status == "pass"
        ):
            preview_identity = nonvisual_preview_evidence(
                factory, repo, pr["number"], args.ticket, head,
            )
            if preview_identity is not None:
                publication_mode = "nonvisual"
            else:
                if preview_provider == "none":
                    raise Refusal("preview_capability_missing")
                publication_mode = "railway"
                preview_urls, preview_identity = railway_preview_evidence(
                    repo, pr["number"], head,
                )
                if preview_identity["status"] != "pass":
                    check_status = "wait"
                    checks = [f"preview identity {preview_identity['reason']}"]
                else:
                    preview_gate = preview_preflight(
                        factory, args.ticket, head, preview_identity,
                    )
                    if preview_gate and preview_gate["status"] != "pass":
                        check_status = (
                            "failed" if preview_gate["status"] == "fail" else "wait"
                        )
                        checks = [f"preview preflight {preview_gate['reason']}"]
        if (
            boundary in {"narrator", "publication"}
            and check_status == "pass"
            and publication_mode != "nonvisual"
            and not preview_urls
        ):
            check_status = "wait"
            checks = ["preview deployment not reported"]
        status = (
            "ready" if boundary in {"narrator", "publication"} and check_status == "pass"
            else "prepared" if check_status == "pass"
            else check_status
        )
        print(json.dumps({
            "boundary": boundary,
            "branch": branch,
            "checks": checks,
            "head": head,
            "pr_number": pr["number"],
            "publication_mode": publication_mode,
            "preview_urls": preview_urls,
            "preview_identity": preview_identity,
            "preview_preflight": preview_gate,
            "schema": SCHEMA,
            "status": status,
            "ticket": args.ticket,
            "url": pr.get("url"),
        }, sort_keys=True, separators=(",", ":")))
    except GitHubUnavailable:
        print(json.dumps({
            "boundary": boundary,
            "branch": branch,
            "checks": ["github unavailable"],
            "head": head,
            "pr_number": pr.get("number"),
            "publication_mode": None,
            "preview_urls": [],
            "preview_identity": None,
            "preview_preflight": None,
            "schema": SCHEMA,
            "status": "wait",
            "ticket": args.ticket,
            "url": pr.get("url"),
        }, sort_keys=True, separators=(",", ":")))
    except (
        FileNotFoundError, json.JSONDecodeError, KeyError, OSError,
        Refusal, subprocess.SubprocessError, UnicodeError, ValueError,
    ) as error:
        print(json.dumps({
            "error": str(error), "schema": SCHEMA, "status": "error",
        }, sort_keys=True, separators=(",", ":")))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
