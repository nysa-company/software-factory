#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts" / "model-control.sh"


class ModelControlTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.state = self.base / "state"
        self.state.mkdir()
        self.product = self.base / "product"
        self.remote = self.base / "product.git"
        self.workdir = self.base / "ticket-T-901"
        (self.product / "factory" / "tickets").mkdir(parents=True)
        self.kit_sha = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        (self.product / "factory" / "KIT_PIN").write_text(self.kit_sha + "\n")
        (self.product / "factory" / "PROJECT.env").write_text(
            "TICKET_BRANCH_PREFIX=ticket/\n"
        )
        (self.product / "factory" / "tickets" / "T-901.md").write_text(
            "# T-901\n\nState: Ready\n"
        )
        subprocess.run(
            ["git", "-C", str(self.product), "init", "-q", "-b", "main"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.product), "config", "user.name", "test"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.product), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.product), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.product), "commit", "-qm", "fixture"],
            check=True,
        )
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(
            ["git", "-C", str(self.product), "remote", "add", "origin", str(self.remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "push", "-q", "-u", "origin", "main"],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.product), "worktree", "add", "-q", "-b",
                "ticket/T-901", str(self.workdir),
            ],
            check=True,
        )
        self.global_env = self.base / "global.env"
        self.global_env.write_text(
            "\n".join(
                (
                    "CODEX_PINNED=0.144.1",
                    "CLAUDE_CODE_PINNED=2.1.207",
                    "FACTORY_CURSOR_FALLBACK_ENABLED=1",
                    "CURSOR_AGENT_VERSION=2026.07.test",
                    "CURSOR_OPENAI_MODEL=gpt-5.6-sol-high",
                    "CURSOR_ANTHROPIC_MODEL=claude-sonnet-5-thinking-high",
                    "FACTORY_PROBE_CODEX=READY:test",
                    "FACTORY_PROBE_CLAUDE_CODE=READY:test",
                    "FACTORY_PROBE_CURSOR_OPENAI=READY:test",
                    "FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test",
                    "",
                )
            )
        )
        self.environment = {
            **os.environ,
            "FACTORY_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
            "FACTORY_MODEL_STATE_ROOT": str(self.state),
            "FACTORY_PROJECT": "model-control-test",
            "FACTORY_ROOT": str(self.product),
            "FACTORY_GLOBAL_ENV": str(self.global_env),
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote),
        }

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, *args, check=True):
        result = subprocess.run(
            [str(CONTROL), *args],
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and result.returncode:
            self.fail("model-control failed: %s %s" % (result.stdout, result.stderr))
        return result

    def model_policy(self):
        return {
            "checking_family": "anthropic",
            "production_family": "openai",
            "roles": {
                "planner": {
                    "effort": "high",
                    "primary_route_id": "codex-gpt-5.6-sol",
                    "secondary_route_id": "cursor-gpt-5.6-sol-high",
                },
                **{
                    role: {
                        "effort": "medium",
                        "primary_route_id": "codex-gpt-5.6-terra",
                        "secondary_route_id": "cursor-gpt-5.6-sol-high",
                    }
                    for role in ("builder", "narrator")
                },
                **{
                    role: {
                        "effort": "medium",
                        "primary_route_id": "claude-fable",
                        "secondary_route_id": "cursor-claude-fable-5-thinking-medium",
                    }
                    for role in ("spec-linter", "test-author")
                },
                "reviewer": {
                    "effort": "high",
                    "primary_route_id": "claude-sonnet",
                    "secondary_route_id": "cursor-claude-sonnet-5-thinking-high",
                },
            },
            "schema": "factory-model-policy/v1",
            "version": 1,
        }

    def test_plan_resolves_all_six_roles_and_honors_profile(self):
        default = json.loads(self.command("plan").stdout)
        selected = json.loads(
            self.command("plan", "--profile", "claude-priority-v1").stdout
        )
        self.assertEqual(len(default["selections"]), 6)
        self.assertEqual(default["profile_id"], "cursor-balanced-v2")
        self.assertEqual(selected["profile_id"], "claude-priority-v1")
        self.assertEqual(selected["selections"]["planner"]["adapter"], "claude-code")

    def test_policy_candidates_preview_cas_apply_and_ticket_status_api(self):
        candidates = json.loads(self.command("policy-candidates").stdout)
        self.assertTrue(candidates["reviewer_exception"]["supported"])
        policy = json.dumps(self.model_policy(), sort_keys=True, separators=(",", ":"))
        preview = json.loads(
            self.command("policy-preview", "--policy", policy).stdout
        )
        stale = self.command(
            "policy-apply",
            "--policy", policy,
            "--expected-current-hash", "0" * 64,
            "--approve-hash", preview["preview_hash"],
            check=False,
        )
        self.assertEqual(stale.returncode, 2)
        applied = json.loads(
            self.command(
                "policy-apply",
                "--policy", policy,
                "--expected-current-hash", preview["current_policy_hash"],
                "--approve-hash", preview["preview_hash"],
            ).stdout
        )
        self.assertRegex(applied["policy_hash"], r"^[0-9a-f]{64}$")
        plan = json.loads(self.command("plan").stdout)
        self.assertEqual(plan["schema"], "model-resolution-plan/v2")
        self.assertEqual(plan["profile_id"], "project-policy")

        status = json.loads(
            self.command("ticket-status", "--ticket", "T-901").stdout
        )
        self.assertEqual(status["state"], "Ready")
        self.assertEqual(status["route_plan_status"], "absent")

    def test_pin_records_affinity_and_plan_in_one_commit_pushes_and_is_idempotent(self):
        before = subprocess.check_output(
            ["git", "-C", str(self.workdir), "rev-parse", "HEAD"], text=True
        ).strip()
        value = json.loads(
            self.command(
                "pin", "--ticket", "T-901", "--workdir", str(self.workdir)
            ).stdout
        )
        route_plan = self.workdir / "factory" / "route-plans" / "T-901.json"
        ticket = self.workdir / "factory" / "tickets" / "T-901.md"
        self.assertTrue(value["commit_created"])
        self.assertRegex(value["commit_sha"], r"^[0-9a-f]{40}$")
        self.assertRegex(value["pin_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(ticket.read_text().count("Kit-SHA:"), 1)
        self.assertIn("Kit-SHA: %s" % self.kit_sha, ticket.read_text())
        self.assertEqual(json.loads(route_plan.read_text())["ticket"], "T-901")
        changed = subprocess.check_output(
            [
                "git", "-C", str(self.workdir), "diff-tree", "--no-commit-id",
                "--name-only", "-r", value["commit_sha"],
            ],
            text=True,
        ).splitlines()
        self.assertEqual(
            sorted(changed),
            ["factory/route-plans/T-901.json", "factory/tickets/T-901.md"],
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.workdir), "rev-list", "--count", f"{before}..HEAD"],
                text=True,
            ).strip(),
            "1",
        )
        remote_head = subprocess.check_output(
            [
                "git", "-C", str(self.workdir), "ls-remote", "--heads",
                str(self.remote), "refs/heads/ticket/T-901",
            ],
            text=True,
        ).split()[0]
        self.assertEqual(remote_head, value["commit_sha"])
        status = subprocess.check_output(
            ["git", "-C", str(self.workdir), "status", "--porcelain"], text=True
        )
        self.assertEqual(status, "")

        again = json.loads(
            self.command(
                "pin", "--ticket", "T-901", "--workdir", str(self.workdir)
            ).stdout
        )
        self.assertFalse(again["commit_created"])
        self.assertEqual(again["commit_sha"], value["commit_sha"])
        self.assertEqual(again["pin_hash"], value["pin_hash"])

    def test_pin_rejects_dirty_wrong_branch_and_wrong_remote(self):
        ticket = self.workdir / "factory" / "tickets" / "T-901.md"
        ticket.write_text(ticket.read_text() + "\nlocal change\n")
        result = self.command(
            "pin", "--ticket", "T-901", "--workdir", str(self.workdir),
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("clean", result.stdout)
        subprocess.run(
            ["git", "-C", str(self.workdir), "restore", str(ticket)], check=True
        )

        subprocess.run(
            ["git", "-C", str(self.workdir), "branch", "-m", "ticket/T-902"],
            check=True,
        )
        result = self.command(
            "pin", "--ticket", "T-901", "--workdir", str(self.workdir), check=False
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not match", result.stdout)
        subprocess.run(
            ["git", "-C", str(self.workdir), "branch", "-m", "ticket/T-901"],
            check=True,
        )

        self.environment["FACTORY_CERTIFIED_PRODUCT_ORIGIN"] = str(
            self.base / "wrong.git"
        )
        result = self.command(
            "pin", "--ticket", "T-901", "--workdir", str(self.workdir), check=False
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("certified product origin", result.stdout)

    def test_precommit_failure_restores_ticket_and_leaves_no_staged_state(self):
        self.global_env.write_text("UNSAFE MODEL CONFIG\n")
        before = (self.workdir / "factory" / "tickets" / "T-901.md").read_text()
        result = self.command(
            "pin", "--ticket", "T-901", "--workdir", str(self.workdir), check=False
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe or malformed", result.stdout)
        self.assertEqual(
            (self.workdir / "factory" / "tickets" / "T-901.md").read_text(), before
        )
        self.assertFalse(
            (self.workdir / "factory" / "route-plans" / "T-901.json").exists()
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.workdir), "status", "--porcelain"], text=True
            ),
            "",
        )

    def test_migration_preview_binds_one_current_readiness_probe_per_command(self):
        self.command("pin", "--ticket", "T-901", "--workdir", str(self.workdir))
        release = self.base / "release"
        shutil.copytree(ROOT / "scripts", release / "scripts")
        backend = release / "scripts" / "lib" / "backend-policy.sh"
        backend.write_text(backend.read_text().replace(
            '  if [[ -n "$readiness_output" ]]; then\n',
            '''  if [[ -n "${FACTORY_TEST_MIGRATION_READINESS_COUNTER:-}" ]]; then
    count="$(awk 'NR==1 {print; exit}' "$FACTORY_TEST_MIGRATION_READINESS_COUNTER" 2>/dev/null || true)"
    count=$((${count:-0} + 1))
    printf '%s\\n' "$count" > "$FACTORY_TEST_MIGRATION_READINESS_COUNTER"
    if [[ "$count" -eq 1 ]]; then
      python3 - "$tmp/readiness.json" <<'PY'
import json, sys
path = sys.argv[1]
value = json.load(open(path))
value[sorted(value)[0]]["reason"] = "drift"
with open(path, "w") as handle:
    json.dump(value, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\\n")
PY
    fi
  fi
  if [[ -n "$readiness_output" ]]; then
''',
            1,
        ))
        (release / "integrations" / "hermes").mkdir(parents=True)
        shutil.copy2(
            ROOT / "integrations" / "hermes" / "contract.json",
            release / "integrations" / "hermes" / "contract.json",
        )
        release_tree = subprocess.check_output(
            [
                "bash", "-c", 'source "$1"; factory_directory_tree "$2"', "_",
                str(ROOT / "scripts" / "lib" / "kit-pin.sh"), str(release),
            ],
            text=True,
        ).strip()
        trace = self.base / "migration-probes.trace"
        environment = {
            **self.environment,
            "FACTORY_PROBE_TRACE": str(trace),
            "FACTORY_RELEASE_CONTRACT_VERSION": "1.8.0",
            "FACTORY_RELEASE_PATH": str(release),
            "FACTORY_RELEASE_SHA": self.kit_sha,
            "FACTORY_RELEASE_TREE": release_tree,
        }

        def migrate(*args):
            result = subprocess.run(
                [str(release / "scripts" / "model-control.sh"), *args],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode:
                self.fail("sealed model-control failed: %s %s" % (
                    result.stdout, result.stderr
                ))
            return json.loads(result.stdout)

        preview = migrate(
            "migrate-plan", "--ticket", "T-901", "--workdir", str(self.workdir)
        )
        preview_probes = trace.read_text().splitlines()
        self.assertNotIn("journal", preview)
        self.assertRegex(preview["readiness_sha256"], r"^[0-9a-f]{64}$")
        route_plan = self.workdir / "factory" / "route-plans" / "T-901.json"
        before_head = subprocess.check_output(
            ["git", "-C", str(self.workdir), "rev-parse", "HEAD"], text=True
        )
        before_plan = route_plan.read_bytes()
        drift_environment = {
            **environment,
            "FACTORY_TEST_MIGRATION_READINESS_COUNTER": str(
                self.base / "migration-readiness-counter"
            ),
        }
        drift = subprocess.run(
            [
                str(release / "scripts" / "model-control.sh"), "migrate",
                "--ticket", "T-901", "--workdir", str(self.workdir),
                "--approve-hash", preview["preview_hash"],
                "--readiness-hash", preview["readiness_sha256"],
                "--approved-by", "tester",
            ],
            env=drift_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(drift.returncode, 2)
        self.assertIn("readiness changed after approval", drift.stdout)
        self.assertEqual(route_plan.read_bytes(), before_plan)
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.workdir), "rev-parse", "HEAD"], text=True
            ),
            before_head,
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.workdir), "status", "--porcelain"],
                text=True,
            ),
            "",
        )
        after_drift_probes = trace.read_text().splitlines()
        self.assertEqual(
            len(after_drift_probes) - len(preview_probes), len(preview_probes)
        )
        self.assertEqual(
            (self.base / "migration-readiness-counter").read_text().strip(), "1"
        )
        applied = migrate(
            "migrate", "--ticket", "T-901", "--workdir", str(self.workdir),
            "--approve-hash", preview["preview_hash"],
            "--readiness-hash", preview["readiness_sha256"],
            "--approved-by", "tester",
        )
        self.assertGreaterEqual(len(preview_probes), 6)
        self.assertEqual(
            len(trace.read_text().splitlines()) - len(after_drift_probes),
            len(preview_probes),
        )
        self.assertEqual(applied["preview_hash"], preview["preview_hash"])
        self.assertEqual(
            json.loads(
                route_plan.read_text()
            )["schema"],
            "ticket-model-route-journal/v2",
        )
        bundle = self.workdir / "factory/attestations/T-901/bundle.json"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(json.dumps({"kit_sha": "b" * 40}) + "\n")
        subprocess.run(
            ["git", "-C", str(self.workdir), "add", str(bundle)], check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.workdir), "-c", "user.name=test",
                "-c", "user.email=test@example.com", "commit", "-qm",
                "stale bundle fixture",
            ],
            check=True,
        )
        stale = subprocess.run(
            [
                str(release / "scripts/model-control.sh"), "migrate-plan",
                "--ticket", "T-901", "--workdir", str(self.workdir),
            ],
            env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(stale.returncode, 2)
        self.assertIn(
            "bundle attestation must be invalidated before route migration",
            stale.stdout,
        )


if __name__ == "__main__":
    unittest.main()
