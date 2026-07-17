#!/usr/bin/env python3
"""Network-free trusted ticket attestation regressions."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ticket-attest.py"
KIT_SHA = "a" * 40


def command(*args, cwd=None, env=None, check=True):
    result = subprocess.run(
        args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


class TicketAttestTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="ticket-attest-test."))
        self.product = self.temp / "product"
        self.remote = self.temp / "product.git"
        self.bin = self.temp / "bin"
        self.state = self.temp / "gh.json"
        self.product.mkdir()
        self.bin.mkdir()
        command("git", "init", "--bare", "-q", str(self.remote))
        command("git", "init", "-q", "-b", "main", cwd=self.product)
        command("git", "remote", "add", "origin", str(self.remote), cwd=self.product)
        (self.product / "factory/tickets").mkdir(parents=True)
        (self.product / "factory/runs").mkdir()
        (self.product / "factory/PROJECT.env").write_text(
            "GH_REPO=acme/widget\nDONE_REQUIRED_CHECKS=ci,deploy-production\n"
            "AUTO_MERGE_METHOD=squash\n"
        )
        (self.product / ".gitignore").write_text(
            "factory/runs/\nfactory/runtime-ledger.csv\nfactory/linear-map.json\n"
        )
        ledger_header = (
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,"
            "run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version\n"
        )
        (self.product / "factory/ledger.csv").write_text(ledger_header)
        (self.product / "factory/tickets/T-700.md").write_text(self.ticket("Review"))
        self.commit("base")
        command("git", "push", "-q", "-u", "origin", "main", cwd=self.product)
        command("git", "switch", "-q", "-c", "ticket/T-700", cwd=self.product)
        (self.product / "app.txt").write_text("reviewed code\n")
        self.commit("implementation")
        self.reviewed = self.head()
        (self.product / "factory/tickets/T-700.md").write_text(
            self.ticket("Review") + "\nreviewer round 1: APPROVE\n"
        )
        (self.product / "factory/tickets/T-700-bundle.md").write_text(
            "# Evidence bundle\n"
            "## What this does\nSafe change.\n"
            "## Preview\nLocal preview.\n"
            "## Screenshots\nNo visual change.\n"
            "## Acceptance criteria\nAll pass.\n"
            "## Risk\nLow.\n"
            "## Cost\n1 USD.\n"
            "## Rollback\nRevert PR.\n"
            "Approve to merge, or send back with what is wrong?\n"
        )
        self.commit("narrator bundle")
        command("git", "push", "-q", "-u", "origin", "ticket/T-700", cwd=self.product)
        self.write_runs()
        self.install_fake_gh()
        self.write_state()
        self.env = dict(os.environ)
        self.env.update({
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FACTORY_ROOT": str(self.product),
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote),
            "FACTORY_RELEASE_SHA": KIT_SHA,
            "FAKE_GH_STATE": str(self.state),
            "FAKE_WORKDIR": str(self.product),
        })
        self.workdir = self.product

    def tearDown(self):
        shutil.rmtree(self.temp)

    @staticmethod
    def ticket(state):
        return f"""# T-700

State: {state}
Priority: normal

## Factory checklist
- [x] Reviewer approved
- [ ] Evidence bundle posted
- [ ] Operator approved
- [ ] PR merged and staging confirmed

## Links
- PR:
- Evidence:
"""

    def commit(self, message):
        command("git", "add", ".", cwd=self.product)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", message, cwd=self.product,
        )

    def head(self):
        return command("git", "rev-parse", "HEAD", cwd=self.product).stdout.strip()

    def write_runs(self):
        fields = (
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,"
            "run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version\n"
        )
        rows = []
        for index, role in enumerate(("reviewer", "narrator"), 1):
            run_id = f"{role}-1"
            (self.product / f"factory/runs/{run_id}.meta").write_text(
                f"run_id={run_id}\naccounting_schema=1\naccounting_state=completed\n"
                "reserved_usd=1\ngo_issued=1\nstarted_at=2026-07-17T12:00:00Z\n"
                "prompt_version=1\nturns=1\neffective_cost=0.1\ncost_basis=reported\n"
                f"exit_status=0\nticket=T-700\nrole={role}\nadapter=mock\n"
                "provider_family=anthropic\nselection_reason=primary\nadapter_version=1\n"
                "model_id=mock\n"
                f"role_head_before={self.reviewed}\nterminal_at=2026-07-17T12:0{index}:00Z\n"
            )
            rows.append(
                f"2026-07-17,12:0{index}:00,T-700,{role},mock,1,1,0.1,0,{run_id},"
                "anthropic,mock,primary,reported,1\n"
            )
        (self.product / "factory/runtime-ledger.csv").write_text(fields + "".join(rows))

    def write_state(self, **updates):
        value = {
            "duplicate": False, "wrong_head": False, "merge_fail": False,
            "auto_merge": True, "merged": False, "merge_sha": "b" * 40,
            "pr_head": None, "checks": {"ci": True, "deploy-production": True},
            "check_runs": {},
        }
        value.update(updates)
        self.state.write_text(json.dumps(value))

    def install_fake_gh(self):
        path = self.bin / "gh"
        path.write_text("""#!/usr/bin/env python3
import json, os, subprocess, sys, urllib.parse
from pathlib import Path
s = json.loads(Path(os.environ["FAKE_GH_STATE"]).read_text())
a = sys.argv[1:]
head = subprocess.check_output(["git", "-C", os.environ["FAKE_WORKDIR"], "rev-parse", "HEAD"], text=True).strip()
if a[:2] == ["pr", "list"]:
    state = a[a.index("--state") + 1]
    item = {"number": 7, "headRefName": "ticket/T-700", "baseRefName": "main",
            "headRefOid": ("c" * 40 if s["wrong_head"] else (s.get("pr_head") or head)), "url": "https://example.invalid/pr/7",
            "state": "MERGED" if state == "all" and s["merged"] else "OPEN",
            "mergedAt": "2026-07-17T18:00:00Z" if s["merged"] else None,
            "mergeCommit": {"oid": s["merge_sha"]} if s["merged"] else None}
    print(json.dumps([item, dict(item, number=8)] if s["duplicate"] else [item]))
elif a[:2] == ["pr", "merge"]:
    if s["merge_fail"]: print("auto-merge unavailable", file=sys.stderr); raise SystemExit(1)
    s["merge_argv"] = a
    Path(os.environ["FAKE_GH_STATE"]).write_text(json.dumps(s))
elif a[:2] == ["pr", "view"]:
    print(json.dumps({"number": 7, "headRefOid": head, "state": "OPEN",
                      "mergeStateStatus": "BLOCKED",
                      "autoMergeRequest": {"mergeMethod": "SQUASH"} if s["auto_merge"] else None}))
elif a[:1] == ["api"]:
    if a[1].endswith("/status"):
        print(json.dumps({"statuses": [{"context": k, "state": "success" if v else "failure"}
                                      for k, v in s["checks"].items()]}))
    else:
        query = urllib.parse.urlparse(a[1]).query
        name = urllib.parse.parse_qs(query).get("check_name", [""])[0]
        print(json.dumps({"check_runs": s["check_runs"].get(name, [])}))
else:
    raise SystemExit(2)
""")
        path.chmod(0o755)

    def attest(self, action):
        return command(
            sys.executable, str(SCRIPT), "--ticket", "T-700",
            "--workdir", str(self.workdir), "--action", action,
            env=self.env, check=False,
        )

    def bundle(self):
        result = self.attest("bundle")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def approval_overlay(self, stale=False):
        stamp = "2020-01-01T00:00:00Z" if stale else "2099-01-01T00:00:00Z"
        (self.product / "factory/linear-map.json").write_text(json.dumps({
            "tickets": {"T-700": {"operator": {
                "state": "Approved", "approval": "Linear",
                "state_base": "awaiting approval",
                "observed_at": stamp, "linear_updated_at": stamp,
            }}},
        }))

    def test_bundle_and_approval_happy_path_and_retry(self):
        self.bundle()
        self.approval_overlay()
        result = self.attest("approval")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("State: Approved", (self.product / "factory/tickets/T-700.md").read_text())
        state = json.loads(self.state.read_text())
        self.assertIn("--squash", state["merge_argv"])
        self.assertNotIn("--merge", state["merge_argv"])

    def test_stale_approval_is_refused(self):
        self.bundle()
        self.approval_overlay(stale=True)
        self.assertIn("not newer", self.attest("approval").stderr)

    def test_merge_method_is_required_and_allowlisted(self):
        project = self.product / "factory/PROJECT.env"
        project.write_text(project.read_text().replace(
            "AUTO_MERGE_METHOD=squash\n", "AUTO_MERGE_METHOD=octopus\n",
        ))
        self.assertIn("AUTO_MERGE_METHOD", self.attest("bundle").stderr)

    def test_changed_code_after_review_is_refused(self):
        (self.product / "app.txt").write_text("unreviewed\n")
        self.commit("unreviewed")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.assertIn("changed after", self.attest("bundle").stderr)

    def test_later_request_changes_overrides_earlier_approve(self):
        ticket = self.product / "factory/tickets/T-700.md"
        ticket.write_text(ticket.read_text() + "reviewer round 2: REQUEST CHANGES — regression\n")
        ledger = self.product / "factory/runtime-ledger.csv"
        rows = ledger.read_text()
        for index, role in ((3, "reviewer"), (4, "narrator")):
            run_id = f"{role}-2"
            (self.product / f"factory/runs/{run_id}.meta").write_text(
                f"run_id={run_id}\naccounting_schema=1\naccounting_state=completed\n"
                "exit_status=0\nticket=T-700\n"
                f"role={role}\nrole_head_before={self.reviewed}\n"
                f"terminal_at=2026-07-17T12:0{index}:00Z\n"
            )
            rows += (
                f"2026-07-17,12:0{index}:00,T-700,{role},mock,1,1,0.1,0,{run_id},"
                "anthropic,mock,primary,reported,1\n"
            )
        ledger.write_text(rows)
        self.commit("later rejection")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.assertIn("latest non-void", self.attest("bundle").stderr)

    def test_changed_bundle_after_attestation_is_refused(self):
        self.bundle()
        path = self.product / "factory/tickets/T-700-bundle.md"
        path.write_text(path.read_text() + "changed\n")
        self.commit("tamper bundle")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.approval_overlay()
        self.assertIn("bundle changed", self.attest("approval").stderr)

    def test_wrong_duplicate_and_head_mismatched_prs_are_refused(self):
        for update, message in (
            ({"duplicate": True}, "exactly one"),
            ({"wrong_head": True}, "head"),
        ):
            with self.subTest(update=update):
                self.write_state(**update)
                self.assertIn(message, self.attest("bundle").stderr)

    def test_auto_merge_failure_retains_overlay_and_can_retry(self):
        self.bundle()
        self.approval_overlay()
        self.write_state(merge_fail=True)
        self.assertIn("auto-merge", self.attest("approval").stderr)
        mapping = json.loads((self.product / "factory/linear-map.json").read_text())
        self.assertIn("operator", mapping["tickets"]["T-700"])
        self.write_state()
        self.assertEqual(self.attest("approval").returncode, 0)

    def test_approval_retry_rejects_tampered_receipt_or_later_head(self):
        self.bundle()
        self.approval_overlay()
        self.write_state(merge_fail=True)
        self.assertNotEqual(self.attest("approval").returncode, 0)
        approval = self.product / "factory/attestations/T-700/approval.json"
        value = json.loads(approval.read_text())
        value["reviewed_sha"] = "d" * 40
        approval.write_text(json.dumps(value, sort_keys=True) + "\n")
        self.commit("tamper approval receipt")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.write_state()
        self.assertIn("invalid", self.attest("approval").stderr)

    def test_approval_retry_rejects_unrelated_later_head(self):
        self.bundle()
        self.approval_overlay()
        self.write_state(merge_fail=True)
        self.assertNotEqual(self.attest("approval").returncode, 0)
        (self.product / "unrelated.txt").write_text("later\n")
        self.commit("later unrelated head")
        command("git", "push", "-q", "origin", "ticket/T-700", cwd=self.product)
        self.write_state()
        self.assertIn("invalid", self.attest("approval").stderr)

    def test_auto_merge_unconfirmed_is_refused(self):
        self.bundle()
        self.approval_overlay()
        self.write_state(auto_merge=False)
        self.assertIn("did not confirm", self.attest("approval").stderr)

    def prepare_done(self, **state):
        self.bundle()
        self.approval_overlay()
        self.assertEqual(self.attest("approval").returncode, 0)
        merge_sha = self.head()
        command("git", "branch", "-f", "main", merge_sha, cwd=self.product)
        command("git", "push", "-q", "origin", f"{merge_sha}:refs/heads/main", cwd=self.product)
        self.workdir = self.temp / "closeout"
        command(
            "git", "worktree", "add", "-q", "-b", "chore/t700-closeout",
            str(self.workdir), "origin/main", cwd=self.product,
        )
        command("git", "push", "-q", "-u", "origin", "chore/t700-closeout", cwd=self.workdir)
        self.env["FAKE_WORKDIR"] = str(self.workdir)
        merged_state = {
            "merged": True, "merge_sha": merge_sha, "pr_head": merge_sha,
        }
        merged_state.update(state)
        self.write_state(**merged_state)

    def test_done_refuses_failed_checks_and_merge_not_on_main(self):
        self.prepare_done(checks={"ci": True, "deploy-production": False})
        self.assertIn("missing or unsuccessful", self.attest("done").stderr)
        self.write_state(merged=True, merge_sha=self.head(), checks={"ci": True})
        self.assertIn("missing or unsuccessful", self.attest("done").stderr)
        self.write_state(merged=True, merge_sha="d" * 40)
        self.assertIn("not reachable", self.attest("done").stderr)

    def test_done_happy_path_projects_ledger_and_attests(self):
        self.prepare_done()
        result = self.attest("done")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("State: Done", (self.workdir / "factory/tickets/T-700.md").read_text())
        self.assertTrue((self.workdir / "factory/attestations/T-700/done.json").is_file())

    def test_done_refuses_missing_or_tampered_approval_and_head_mismatch(self):
        self.prepare_done()
        approval = self.workdir / "factory/attestations/T-700/approval.json"
        approval.unlink()
        command("git", "add", "-A", cwd=self.workdir)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "manual approved without receipt", cwd=self.workdir,
        )
        command("git", "push", "-q", "origin", "HEAD:main", cwd=self.workdir)
        self.assertIn("lacks bundle or approval", self.attest("done").stderr)

    def test_done_refuses_tampered_protected_approval_receipt(self):
        self.prepare_done()
        approval = self.workdir / "factory/attestations/T-700/approval.json"
        value = json.loads(approval.read_text())
        value["reviewed_sha"] = "9" * 40
        approval.write_text(json.dumps(value, sort_keys=True) + "\n")
        command("git", "add", approval, cwd=self.workdir)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "tamper protected approval", cwd=self.workdir,
        )
        command("git", "push", "-q", "origin", "HEAD:main", cwd=self.workdir)
        self.assertIn("protected approval evidence", self.attest("done").stderr)

    def test_done_refuses_merged_head_mismatch_and_check_name_collision(self):
        self.prepare_done(pr_head=self.reviewed)
        self.assertNotEqual(self.attest("done").returncode, 0)
        approval_head = command(
            "git", "rev-parse", "origin/main", cwd=self.workdir,
        ).stdout.strip()
        self.write_state(
            merged=True, merge_sha=approval_head, pr_head=approval_head,
            check_runs={"ci": [{
                "name": "ci", "status": "completed", "conclusion": "success",
            }]},
        )
        self.assertIn("ambiguous", self.attest("done").stderr)
        duplicate = {
            "name": "ci", "status": "completed", "conclusion": "success",
        }
        self.write_state(
            merged=True, merge_sha=approval_head, pr_head=approval_head,
            checks={"deploy-production": True},
            check_runs={"ci": [duplicate, dict(duplicate)]},
        )
        self.assertIn("multiple latest", self.attest("done").stderr)

    def test_done_refuses_closeout_commit_before_projection(self):
        self.prepare_done()
        (self.workdir / "arbitrary.txt").write_text("not closeout evidence\n")
        command("git", "add", ".", cwd=self.workdir)
        command(
            "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "arbitrary closeout commit", cwd=self.workdir,
        )
        self.assertIn("exactly at origin/main", self.attest("done").stderr)

    def test_dispatch_lease_wrapper_requires_matching_opaque_lease_at_two(self):
        project = self.product / "factory/PROJECT.env"
        wrapper = ROOT / "scripts/ticket-attest.sh"
        env = dict(self.env)
        env.pop("FACTORY_DISPATCH_LEASE_ID", None)
        single = command(
            "bash", str(wrapper), "--ticket", "T-700", "--workdir",
            str(self.product), "--action", "invalid", env=env, check=False,
        )
        self.assertNotIn("dispatcher lease", single.stderr)
        project.write_text(project.read_text() + "MAX_CONCURRENT_TICKETS=2\n")
        lease = "1" * 64
        lease_dir = self.product / "factory/.dispatch-leases"
        lease_dir.mkdir()
        (lease_dir / "T-700.json").write_text(json.dumps({
            "schema_version": 1, "ticket": "T-700", "lease_id": lease,
            "expires_epoch": 4102444800,
        }))
        missing = command(
            "bash", str(wrapper), "--ticket", "T-700", "--workdir",
            str(self.product), "--action", "bundle", env=env, check=False,
        )
        self.assertIn("canonical dispatcher lease", missing.stderr)
        env["FACTORY_DISPATCH_LEASE_ID"] = "2" * 64
        wrong = command(
            "bash", str(wrapper), "--ticket", "T-700", "--workdir",
            str(self.product), "--action", "bundle", env=env, check=False,
        )
        self.assertIn("does not match", wrong.stderr)
        env["FACTORY_DISPATCH_LEASE_ID"] = lease
        matching = command(
            "bash", str(wrapper), "--ticket", "T-700", "--workdir",
            str(self.product), "--action", "bundle", env=env, check=False,
        )
        self.assertNotIn("dispatcher lease", matching.stderr)
        self.assertNotIn(lease, missing.stdout + missing.stderr + wrong.stdout + wrong.stderr)


if __name__ == "__main__":
    unittest.main()
