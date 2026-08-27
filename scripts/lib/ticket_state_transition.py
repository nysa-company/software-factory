#!/usr/bin/env python3
"""Pure ticket-state transition policy and text mutation helpers."""

from __future__ import annotations

import os
from pathlib import Path
import re


FACTORY_TARGET_STATES = {
    "ready": "Ready",
    "planning": "Planning",
    "building": "Building",
    "review": "Review",
    "awaiting approval": "Awaiting Approval",
    "blocked-escalated": "Blocked-Escalated",
    "approved": "Approved",
    "done": "Done",
}

LIFECYCLE_STATES = frozenset({
    "backlog", "ready", "planning", "building", "review",
    "awaiting approval", "approved", "blocked-escalated", "done",
    "canceled",
})

ALLOWED_TRANSITIONS = {
    "materialize": {
        ("backlog", "ready"),
        ("backlog", "canceled"),
        *(("blocked-escalated", state) for state in (
            "backlog", "ready", "planning", "building", "review",
        )),
    },
    "transition": {
        ("ready", "planning"),
        ("planning", "building"),
        ("building", "review"),
        ("review", "building"),
        *((state, "blocked-escalated") for state in (
            "ready", "planning", "building", "review",
            "awaiting approval", "approved",
        )),
    },
    "reviewer-reconcile": {("review", "building")},
    "qualification-backlog": {
        ("planning", "backlog"),
        ("building", "backlog"),
    },
}


class TransitionError(ValueError):
    """A ticket transition violates the trusted state policy."""


CONTROL_LINE = re.compile(
    r"(?:"
    r"\s*SPEC-LINT:\s*(?:PASS|FAIL)(?:\s+—\s+.*)?\s*"
    r"|\s*reviewer round\s+[1-9][0-9]*:\s*"
    r"(?:APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)\s*"
    r"|\s*reviewer round\s+[1-9][0-9]*\s+FIX-OWNER:\s*"
    r"(?:builder|test-author|both)\s*"
    r"|\s*OPERATOR NOTE:\s*reviewer run\s*[1-9][0-9]*\s+"
    r"void[^A-Za-z0-9]*duplicate\s*"
    r"|\s*OPERATOR AUTHORIZATION:\s*"
    r"(?:planner|spec-linter|test-author|builder|reviewer|narrator)\s+"
    r"round\s+[1-9][0-9]*\s*"
    r")",
    re.IGNORECASE,
)

RESUME_CONTROL_LINE = re.compile(
    r"(?:"
    r"\s*OPERATOR RESUME:\s*(?:planner|spec-linter|test-author|builder)\s*"
    r"|\s*OPERATOR RESUME RECEIPT:\s*[0-9a-f]{64}\s*"
    r")",
)


def protocol_controls(text: str) -> list[str]:
    return [
        line for line in text.splitlines(keepends=True)
        if CONTROL_LINE.fullmatch(line.rstrip("\r\n"))
    ]


def planner_spec_linter_authorization(text: str) -> tuple[int, str] | None:
    """Return the next gated round and whether its one-use grant is armed."""
    failures = 0
    required = 0
    armed = 0
    invalid = False
    for line in protocol_controls(text):
        verdict = re.fullmatch(
            r"\s*SPEC-LINT:\s*(PASS|FAIL)(?:\s+—\s+.*)?\s*",
            line.rstrip("\r\n"),
            re.IGNORECASE,
        )
        if verdict:
            if required:
                if armed != required:
                    invalid = True
                else:
                    armed = 0
                    required += 1
            if verdict[1].upper() == "FAIL":
                failures += 1
                if failures == 2 and required == 0:
                    required = 3
            continue
        grant = re.fullmatch(
            r"OPERATOR AUTHORIZATION: spec-linter round ([1-9][0-9]*)",
            line.rstrip("\r\n"),
        )
        if grant and required:
            semantic_round = int(grant[1])
            if semantic_round != required:
                continue
            if armed:
                invalid = True
            else:
                armed = semantic_round
    if not required:
        return None
    return required, "invalid" if invalid else "authorized" if armed else "required"


def fresh_protocol_text(current: str, baseline: str) -> str:
    protected = protocol_controls(baseline)
    remaining = iter(protected)
    expected = next(remaining, None)
    fresh: list[str] = []
    for line in current.splitlines(keepends=True):
        if expected is not None and CONTROL_LINE.fullmatch(line.rstrip("\r\n")):
            if line != expected:
                raise TransitionError(
                    "protected qualification role-control history changed"
                )
            expected = next(remaining, None)
        else:
            fresh.append(line)
    if expected is not None:
        raise TransitionError(
            "protected qualification role-control history changed"
        )
    return "".join(fresh)


def fresh_resume_text(current: str, baseline: str) -> str:
    protected = [
        line for line in baseline.splitlines(keepends=True)
        if RESUME_CONTROL_LINE.fullmatch(line.rstrip("\r\n"))
    ]
    remaining = iter(protected)
    expected = next(remaining, None)
    fresh: list[str] = []
    for line in current.splitlines(keepends=True):
        if expected is not None and RESUME_CONTROL_LINE.fullmatch(
            line.rstrip("\r\n")
        ):
            if line != expected:
                raise TransitionError(
                    "blocked transition resume history changed"
                )
            expected = next(remaining, None)
        else:
            fresh.append(line)
    if expected is not None:
        raise TransitionError("blocked transition resume history changed")
    return "".join(fresh)


def qualification_epoch_text(product: Path, ticket: str, current: str) -> str:
    if os.environ.get("FACTORY_KIT_TRUST_SCOPE") != "qualification-candidate":
        return current
    sha = os.environ.get("FACTORY_QUALIFICATION_PRODUCT_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(
        r"T-[0-9]+", ticket
    ):
        raise TransitionError("qualification role-control baseline is invalid")
    mode = os.environ.get("FACTORY_QUALIFICATION_MODE", "")
    if mode == "isolated":
        release_path = os.environ.get("FACTORY_RELEASE_PATH", "")
        project = os.environ.get("FACTORY_PROJECT", "")
        factory_root = os.environ.get("FACTORY_ROOT", "")
        receipt_id = os.environ.get("FACTORY_QUALIFICATION_RECEIPT_ID", "")
        current_tree = os.environ.get("FACTORY_QUALIFICATION_PRODUCT_TREE", "")
        if (
            not Path(release_path).is_absolute()
            or not Path(factory_root).is_absolute()
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt_id)
            or not re.fullmatch(r"[0-9a-f]{40}", current_tree)
        ):
            raise TransitionError("qualification role-control baseline is invalid")
        from qualification_release import ReceiptError, role_control_epoch

        try:
            sha, _tree = role_control_epoch(
                Path(release_path), project, Path(factory_root), receipt_id,
                sha, current_tree,
            )
        except (OSError, ReceiptError) as error:
            raise TransitionError(
                "qualification role-control baseline is invalid"
            ) from error
    elif mode != "takeover":
        raise TransitionError("qualification role-control baseline is invalid")
    from legacy_closeout import _git_object

    try:
        value = _git_object(product, f"{sha}:factory/tickets/{ticket}.md")
    except OSError as error:
        raise TransitionError(
            "qualification role-control baseline is unavailable"
        ) from error
    if value is None or value[1] != "blob":
        raise TransitionError("qualification role-control baseline is unavailable")
    try:
        baseline = value[2].decode("utf-8")
    except UnicodeDecodeError as error:
        raise TransitionError(
            "qualification role-control baseline is not UTF-8"
        ) from error
    return fresh_protocol_text(current, baseline)


def field(text: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}:\s*(.+)$",
        text,
        re.MULTILINE | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def parse_state(text: str) -> str:
    """Return the one canonical lifecycle state declared by ticket text."""
    states = re.findall(
        r"^State:\s*(.*?)\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if len(states) != 1:
        raise TransitionError("ticket State field is ambiguous")
    state = states[0].lower()
    if state not in LIFECYCLE_STATES:
        raise TransitionError("ticket State field is invalid")
    return state


def exact_state(text: str) -> str:
    return parse_state(text)


def validate_materialization(current_text: str, effective_text: str) -> None:
    current_state = parse_state(current_text)
    effective_state = parse_state(effective_text)
    if (
        current_state != effective_state
        and effective_state in {"awaiting approval", "done"}
    ):
        raise TransitionError(
            "evidence-sensitive state requires a dedicated attestation: "
            f"{effective_state}"
        )
    current_approval = field(current_text, "Operator-Approval")
    effective_approval = field(effective_text, "Operator-Approval")
    if (
        current_state in {"awaiting approval", "approved"}
        or effective_state in {"awaiting approval", "approved"}
        or current_approval
        or effective_approval
    ):
        raise TransitionError(
            "approval materialization requires the unavailable dedicated "
            "bundle-attestation path"
        )
    resume_state = field(current_text, "Resume-State").lower()
    legal_resume = (
        current_state == "blocked-escalated"
        and effective_state == resume_state
        and effective_state in {
            "backlog", "ready", "planning", "building", "review",
        }
    )
    legal_ready = current_state == "backlog" and effective_state == "ready"
    legal_cancel = current_state == "backlog" and effective_state == "canceled"
    if current_state != effective_state and not (
        legal_ready or legal_cancel or legal_resume
    ):
        raise TransitionError(
            "operator overlay cannot materialize a factory-owned state transition"
        )


def apply_factory_transition(text: str, target: str, contract: str) -> str:
    current = parse_state(text)
    target_key = target.strip().lower()
    if target_key not in FACTORY_TARGET_STATES:
        raise TransitionError(f"illegal factory transition target: {target_key}")
    text = re.sub(
        r"^State:\s*.*$",
        f"State: {FACTORY_TARGET_STATES[target_key]}",
        text,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if target_key == "blocked-escalated" and contract in {"1.7.0", "1.8.0", "2.0.0"}:
        resume = f"Resume-State: {FACTORY_TARGET_STATES[current]}"
        resume_fields = re.findall(
            r"^Resume-State:\s*.*$",
            text,
            re.MULTILINE | re.IGNORECASE,
        )
        if len(resume_fields) > 1:
            raise TransitionError("ticket contains duplicate Resume-State fields")
        if resume_fields:
            text = re.sub(
                r"^Resume-State:\s*.*$",
                resume,
                text,
                count=1,
                flags=re.MULTILINE | re.IGNORECASE,
            )
        else:
            text = re.sub(
                r"^(State:\s*.*)$",
                rf"\1\n{resume}",
                text,
                count=1,
                flags=re.MULTILINE | re.IGNORECASE,
            )
    return text


def validate_action_transition(action: str, initial: str, final: str) -> None:
    initial = initial.strip().lower()
    final = final.strip().lower()
    if initial != final and (initial, final) not in ALLOWED_TRANSITIONS[action]:
        raise TransitionError(
            f"illegal {action} transition: {initial} -> {final}"
        )
