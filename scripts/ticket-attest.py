#!/usr/bin/env python3
"""Evidence-bound ticket approval, protected auto-merge, and closeout."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class Refusal(ValueError):
    pass


def run(argv, *, cwd=None, input_text=None, check=True):
    result = subprocess.run(
        argv, cwd=cwd, input=input_text, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise Refusal(result.stderr.strip() or result.stdout.strip() or f"{argv[0]} failed")
    return result


def git(root, *args, check=True):
    return run(["git", "-C", str(root), *args], check=check)


def gh(*args):
    return run(["gh", *args])


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp(value, label):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise Refusal(f"invalid {label} timestamp")


def parse_project(path):
    allowed = {"GH_REPO", "DONE_REQUIRED_CHECKS", "TICKET_BRANCH_PREFIX"}
    values = {}
    if not path.is_file() or path.is_symlink():
        raise Refusal("factory/PROJECT.env is missing or unsafe")
    assignment = re.compile(r"(?:export[ \t]+)?([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*(.*)")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = assignment.fullmatch(line)
        if not match or match.group(1) not in allowed:
            continue
        key, encoded = match.groups()
        if key in values:
            raise Refusal(f"duplicate product field {key}")
        if encoded[:1] in ("'", '"'):
            if len(encoded) < 2 or encoded[-1] != encoded[0] or encoded[0] in encoded[1:-1]:
                raise Refusal(f"unsafe product field {key}")
            encoded = encoded[1:-1]
        if any(fragment in encoded for fragment in ("`", "$(", "${", "\\", "\n", "\r")):
            raise Refusal(f"unsafe product field {key}")
        values[key] = encoded
    repo = values.get("GH_REPO", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise Refusal("GH_REPO must be an exact owner/repository slug")
    prefix = values.get("TICKET_BRANCH_PREFIX", "ticket/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*/", prefix) or any(
        part in prefix for part in ("..", "//", "@{", "\\", "~", "^", ":")
    ):
        raise Refusal("TICKET_BRANCH_PREFIX is invalid")
    checks = values.get("DONE_REQUIRED_CHECKS", "")
    if checks:
        names = checks.split(",")
        if any(
            name != name.strip()
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:/()=-]{0,199}", name)
            for name in names
        ) or len(names) != len(set(names)):
            raise Refusal("DONE_REQUIRED_CHECKS must be a unique comma-separated exact-name list")
    else:
        names = []
    return repo, prefix, names


def meta(path):
    values = {}
    for line in path.read_text().splitlines():
        if "=" not in line:
            raise Refusal(f"malformed run manifest {path.name}")
        key, value = line.split("=", 1)
        if key in values:
            raise Refusal(f"duplicate manifest field {key}")
        values[key] = value
    return values


def successful_runs(product, ticket):
    manifests = []
    runs = product / "factory" / "runs"
    if not runs.is_dir() or runs.is_symlink():
        raise Refusal("authoritative run manifest directory is missing")
    for path in sorted(runs.glob("*.meta")):
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise Refusal("run manifests must be regular single-link files")
        value = meta(path)
        if (
            value.get("ticket") == ticket
            and value.get("accounting_state") == "completed"
            and value.get("exit_status") == "0"
        ):
            manifests.append(value)
    ledger = product / "factory" / "runtime-ledger.csv"
    if not ledger.is_file() or ledger.is_symlink():
        raise Refusal("effective runtime ledger is missing")
    with ledger.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    successful_ids = {
        row.get("run_id") for row in rows
        if row.get("ticket") == ticket and row.get("exit_status") == "0"
    }
    for value in manifests:
        if value.get("run_id") not in successful_ids:
            raise Refusal(f"successful manifest {value.get('run_id')} is absent from ledger")
    return manifests


def exact_pr(repo, branch, state):
    fields = "number,headRefName,baseRefName,headRefOid,url,state,mergedAt,mergeCommit"
    result = json.loads(gh(
        "pr", "list", "--repo", repo, "--state", state, "--head", branch,
        "--base", "main", "--json", fields,
    ).stdout)
    if not isinstance(result, list) or len(result) != 1:
        raise Refusal(f"expected exactly one {state} PR for {repo}:{branch} -> main")
    pr = result[0]
    if pr.get("headRefName") != branch or pr.get("baseRefName") != "main":
        raise Refusal("GitHub returned a PR with the wrong head or base")
    return pr


def ensure_clean_branch(product, workdir, expected, *, based_on_main=False, require_remote=True):
    if git(workdir, "status", "--porcelain", "--untracked-files=all").stdout:
        raise Refusal("attestation worktree must be clean")
    branch = git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if branch != expected:
        raise Refusal(f"attestation worktree must be on {expected}")
    local = git(workdir, "rev-parse", "HEAD").stdout.strip()
    if require_remote:
        remote = git(workdir, "rev-parse", f"refs/remotes/origin/{branch}", check=False)
        if remote.returncode or remote.stdout.strip() != local:
            raise Refusal("local branch must exactly match its origin tracking tip")
    if based_on_main and git(
        workdir, "merge-base", "--is-ancestor", "origin/main", "HEAD", check=False
    ).returncode:
        raise Refusal("closeout branch must be based on origin/main")
    return local


def field(text, name):
    matches = re.findall(rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.I | re.M)
    if len(matches) != 1:
        raise Refusal(f"ticket must contain exactly one {name} field")
    return matches[0]


def replace_field(text, name, value):
    return re.sub(
        rf"^{re.escape(name)}:\s*.*$", f"{name}: {value}", text,
        count=1, flags=re.I | re.M,
    )


def check_item(text, label):
    pattern = rf"^- \[[ xX]\] {re.escape(label)}\s*$"
    if re.search(pattern, text, re.M):
        return re.sub(pattern, f"- [x] {label}", text, count=1, flags=re.M)
    return text


def set_link(text, label, value):
    pattern = rf"^- {re.escape(label)}:\s*.*$"
    if re.search(pattern, text, re.M):
        return re.sub(pattern, f"- {label}: {value}", text, count=1, flags=re.M)
    return text


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def commit_push(product, workdir, remote, branch, message, paths):
    for path in paths:
        git(workdir, "add", "--", str(path.relative_to(workdir)))
    git(
        workdir, "-c", "user.name=Software Factory", "-c",
        "user.email=factory@local", "commit", "-m", message,
    )
    head = git(workdir, "rev-parse", "HEAD").stdout.strip()
    configured = git(product, "remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
    if configured != [remote]:
        raise Refusal("configured origin no longer matches the certified product origin")
    git(workdir, "push", "--no-force", "--", remote, f"{head}:refs/heads/{branch}")
    observed = git(workdir, "ls-remote", "--heads", "--", remote, f"refs/heads/{branch}").stdout
    if observed.split()[:1] != [head]:
        raise Refusal("remote did not confirm the attestation commit")
    git(workdir, "update-ref", f"refs/remotes/origin/{branch}", head)
    return head


def consume_overlay(product, ticket, expected_version):
    path = product / "factory" / "linear-map.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text())
    entry = data.get("tickets", {}).get(ticket, {})
    operator = entry.get("operator") or {}
    actual = hashlib.sha256(json.dumps(
        {key: operator[key] for key in ("priority", "initiative", "state", "approval") if key in operator},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if actual != expected_version:
        raise Refusal("operator overlay changed before consumption")
    entry.pop("operator", None)
    fd, temporary = tempfile.mkstemp(prefix=".linear-map.", dir=path.parent)
    with os.fdopen(fd, "w") as output:
        json.dump(data, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def bundle(args, product, workdir, repo, prefix, remote, kit_sha):
    branch = f"{prefix}{args.ticket}"
    head = ensure_clean_branch(product, workdir, branch)
    ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
    bundle_path = workdir / "factory" / "tickets" / f"{args.ticket}-bundle.md"
    text = ticket_path.read_text()
    if field(text, "State").lower() != "review":
        raise Refusal("bundle requires ticket State Review")
    bundle_text = bundle_path.read_text()
    required = (
        "What this does", "Preview", "Acceptance criteria", "Risk", "Cost", "Rollback",
    )
    if any(not re.search(rf"^#+\s+.*{re.escape(section)}", bundle_text, re.I | re.M) for section in required):
        raise Refusal("evidence bundle is missing a required section")
    manifests = successful_runs(product, args.ticket)
    reviewers = [item for item in manifests if item.get("role") == "reviewer"]
    narrators = [item for item in manifests if item.get("role") == "narrator"]
    if not reviewers or not narrators:
        raise Refusal("successful reviewer and narrator evidence is required")
    reviewer = max(reviewers, key=lambda item: item.get("terminal_at", ""))
    narrator = max(narrators, key=lambda item: item.get("terminal_at", ""))
    reviewed = reviewer.get("role_head_before", "")
    if not re.fullmatch(r"[0-9a-f]{40}", reviewed):
        raise Refusal("latest reviewer manifest lacks a reviewed SHA")
    if not re.search(r"^reviewer round\s+\d+:\s*APPROVE\s*$", text, re.I | re.M):
        raise Refusal("ticket lacks reviewer APPROVE verdict")
    allowed = {
        f"factory/tickets/{args.ticket}.md",
        f"factory/tickets/{args.ticket}-bundle.md",
    }
    changed = set(git(workdir, "diff", "--name-only", f"{reviewed}..{head}").stdout.splitlines())
    if not changed or changed - allowed:
        raise Refusal("product or code changed after the reviewed SHA")
    pr = exact_pr(repo, branch, "open")
    if pr.get("headRefOid") != head:
        raise Refusal("PR head does not match the exact ticket branch")
    blob = git(workdir, "hash-object", str(bundle_path)).stdout.strip()
    attestation_path = workdir / "factory" / "attestations" / args.ticket / "bundle.json"
    attestation = {
        "schema": "nysa.software-factory.ticket-bundle/v1",
        "ticket": args.ticket,
        "repository": repo,
        "branch": branch,
        "branch_head": head,
        "reviewed_sha": reviewed,
        "bundle_path": str(bundle_path.relative_to(workdir)),
        "bundle_blob": blob,
        "pr_number": pr["number"],
        "pr_url": pr.get("url"),
        "reviewer_run_id": reviewer["run_id"],
        "narrator_run_id": narrator["run_id"],
        "kit_sha": kit_sha,
        "attested_at": now(),
    }
    write_json(attestation_path, attestation)
    text = replace_field(text, "State", "Awaiting Approval")
    text = check_item(text, "Evidence bundle posted")
    text = set_link(text, "PR", pr.get("url") or f"#{pr['number']}")
    text = set_link(text, "Evidence", str(bundle_path.relative_to(workdir)))
    ticket_path.write_text(text)
    result_head = commit_push(
        product, workdir, remote, branch, f"{args.ticket}: attest operator bundle",
        (ticket_path, attestation_path),
    )
    return {"action": "bundle", "head": result_head, "attestation": attestation}


def approval(args, product, workdir, repo, prefix, remote, kit_sha):
    branch = f"{prefix}{args.ticket}"
    head = ensure_clean_branch(product, workdir, branch)
    ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
    bundle_path = workdir / "factory" / "tickets" / f"{args.ticket}-bundle.md"
    attestation_path = workdir / "factory" / "attestations" / args.ticket / "bundle.json"
    approval_path = attestation_path.with_name("approval.json")
    bundle_att = json.loads(attestation_path.read_text())
    if bundle_att.get("schema") != "nysa.software-factory.ticket-bundle/v1":
        raise Refusal("bundle attestation schema is invalid")
    if git(workdir, "hash-object", str(bundle_path)).stdout.strip() != bundle_att.get("bundle_blob"):
        raise Refusal("evidence bundle changed after attestation")
    if bundle_att.get("ticket") != args.ticket or bundle_att.get("repository") != repo:
        raise Refusal("bundle attestation identity mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", bundle_att.get("branch_head", "")):
        raise Refusal("bundle attestation branch evidence is invalid")
    mapping = json.loads((product / "factory" / "linear-map.json").read_text())
    operator = mapping.get("tickets", {}).get(args.ticket, {}).get("operator") or {}
    existing_approval = json.loads(approval_path.read_text()) if approval_path.exists() else None
    exact_overlay = (
        operator.get("state") == "Approved"
        and operator.get("approval") == "Linear"
        and operator.get("state_base") == "awaiting approval"
    )
    if not exact_overlay and not existing_approval:
        raise Refusal("exact Linear Awaiting Approval -> Approved overlay is required")
    if exact_overlay:
        observed = timestamp(operator.get("observed_at"), "approval observation")
        updated = timestamp(operator.get("linear_updated_at"), "Linear approval update")
        attested = timestamp(bundle_att.get("attested_at"), "bundle attestation")
        if observed <= attested or updated <= attested:
            raise Refusal("Linear approval is not newer than the bundle attestation")
        version = hashlib.sha256(json.dumps(
            {key: operator[key] for key in ("priority", "initiative", "state", "approval") if key in operator},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
    else:
        version = existing_approval.get("operator_version", "")
    pr = exact_pr(repo, branch, "open")
    if pr.get("number") != bundle_att.get("pr_number") or pr.get("headRefOid") != head:
        raise Refusal("PR identity or head changed before approval")
    if existing_approval:
        approval_att = existing_approval
        if approval_att.get("operator_version") != version:
            raise Refusal("existing approval attestation does not match the overlay")
    else:
        text = ticket_path.read_text()
        if field(text, "State").lower() != "awaiting approval":
            raise Refusal("approval requires committed Awaiting Approval state")
        parent = git(workdir, "rev-parse", "HEAD^").stdout.strip()
        expected_paths = {
            f"factory/tickets/{args.ticket}.md",
            f"factory/attestations/{args.ticket}/bundle.json",
        }
        actual_paths = set(git(workdir, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.splitlines())
        if parent != bundle_att["branch_head"] or actual_paths != expected_paths:
            raise Refusal("bundle attestation commit or branch evidence changed")
        text = replace_field(text, "State", "Approved")
        if re.search(r"^Operator-Approval:", text, re.I | re.M):
            text = replace_field(text, "Operator-Approval", "Linear")
        else:
            text = re.sub(r"^(State:.*)$", r"\1\nOperator-Approval: Linear", text, count=1, flags=re.M)
        text = check_item(text, "Operator approved")
        ticket_path.write_text(text)
        approval_att = {
            "schema": "nysa.software-factory.ticket-approval/v1",
            "ticket": args.ticket,
            "repository": repo,
            "branch": branch,
            "parent_head": head,
            "reviewed_sha": bundle_att["reviewed_sha"],
            "bundle_blob": bundle_att["bundle_blob"],
            "bundle_attestation_blob": git(workdir, "hash-object", str(attestation_path)).stdout.strip(),
            "pr_number": pr["number"],
            "operator_version": version,
            "linear_updated_at": operator["linear_updated_at"],
            "observed_at": operator["observed_at"],
            "kit_sha": kit_sha,
            "attested_at": now(),
        }
        write_json(approval_path, approval_att)
        head = commit_push(
            product, workdir, remote, branch, f"{args.ticket}: attest Linear approval",
            (ticket_path, approval_path),
        )
    current = exact_pr(repo, branch, "open")
    if current.get("number") != approval_att["pr_number"] or current.get("headRefOid") != head:
        raise Refusal("PR head changed before auto-merge request")
    merged = gh(
        "pr", "merge", str(current["number"]), "--repo", repo, "--auto", "--merge",
    )
    if merged.returncode:
        raise Refusal("GitHub did not accept protected auto-merge")
    view = json.loads(gh(
        "pr", "view", str(current["number"]), "--repo", repo,
        "--json", "number,headRefOid,autoMergeRequest,state,mergeStateStatus",
    ).stdout)
    if view.get("headRefOid") != head or (
        view.get("state") != "MERGED" and not view.get("autoMergeRequest")
    ):
        raise Refusal("GitHub did not confirm auto-merge for the exact approved head")
    if exact_overlay:
        consume_overlay(product, args.ticket, version)
    return {"action": "approval", "head": head, "pr_number": current["number"], "auto_merge": True}


def done(args, product, workdir, repo, prefix, remote, checks, kit_sha):
    branch = f"chore/{args.ticket.lower().replace('-', '')}-closeout"
    ensure_clean_branch(product, workdir, branch, based_on_main=True, require_remote=False)
    pr = exact_pr(repo, f"{prefix}{args.ticket}", "all")
    if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        raise Refusal("ticket PR is not merged")
    merge = (pr.get("mergeCommit") or {}).get("oid", "")
    if not re.fullmatch(r"[0-9a-f]{40}", merge):
        raise Refusal("merged PR lacks an exact merge commit")
    if git(workdir, "merge-base", "--is-ancestor", merge, "origin/main", check=False).returncode:
        raise Refusal("PR merge commit is not reachable from authoritative origin/main")
    combined = json.loads(gh("api", f"repos/{repo}/commits/{merge}/status").stdout)
    runs = json.loads(gh("api", f"repos/{repo}/commits/{merge}/check-runs").stdout)
    observed = {}
    for status in combined.get("statuses", []):
        observed[status.get("context")] = status.get("state") == "success"
    for item in runs.get("check_runs", []):
        observed[item.get("name")] = item.get("status") == "completed" and item.get("conclusion") == "success"
    missing = [name for name in checks if observed.get(name) is not True]
    if missing:
        raise Refusal("required post-merge checks are missing or unsuccessful: " + ", ".join(missing))
    ledger = Path(__file__).with_name("ledger-view.py")
    projection = run([
        sys.executable, "-I", "-S", str(ledger), "project",
        "--factory-root", str(product), "--workdir", str(workdir), "--ticket", args.ticket,
    ])
    ledger_result = json.loads(projection.stdout)
    ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
    text = ticket_path.read_text()
    if field(text, "State").lower() != "approved":
        raise Refusal("closeout requires an Approved ticket on protected main")
    text = replace_field(text, "State", "Done")
    text = check_item(text, "PR merged and staging confirmed")
    ticket_path.write_text(text)
    done_path = workdir / "factory" / "attestations" / args.ticket / "done.json"
    done_att = {
        "schema": "nysa.software-factory.ticket-done/v1",
        "ticket": args.ticket,
        "repository": repo,
        "pr_number": pr["number"],
        "merge_commit": merge,
        "merged_at": pr["mergedAt"],
        "required_checks": checks,
        "successful_checks": sorted(name for name in checks if observed.get(name)),
        "ledger": ledger_result,
        "kit_sha": kit_sha,
        "attested_at": now(),
    }
    write_json(done_path, done_att)
    head = commit_push(
        product, workdir, remote, branch, f"{args.ticket}: record protected merge closeout",
        (workdir / "factory" / "ledger.csv", ticket_path, done_path),
    )
    return {"action": "done", "head": head, "attestation": done_att}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--action", choices=("bundle", "approval", "done"), required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"T-\d+", args.ticket):
        parser.error("invalid ticket identifier")
    product = Path(os.environ["FACTORY_ROOT"]).resolve()
    workdir = Path(args.workdir).resolve()
    remote = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    kit_sha = os.environ.get("FACTORY_RELEASE_SHA", "")
    if not remote or not re.fullmatch(r"[0-9a-f]{40}", kit_sha):
        raise Refusal("trusted launcher evidence is unavailable")
    repo, prefix, checks = parse_project(product / "factory" / "PROJECT.env")
    if args.action == "bundle":
        result = bundle(args, product, workdir, repo, prefix, remote, kit_sha)
    elif args.action == "approval":
        result = approval(args, product, workdir, repo, prefix, remote, kit_sha)
    else:
        result = done(args, product, workdir, repo, prefix, remote, checks, kit_sha)
    print(json.dumps({"status": "ok", "ticket": args.ticket, **result}, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, json.JSONDecodeError, Refusal) as error:
        print(f"ticket-attest: {error}", file=sys.stderr)
        raise SystemExit(1)
