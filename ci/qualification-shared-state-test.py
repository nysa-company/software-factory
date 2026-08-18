#!/usr/bin/env python3
"""Credential-free shared-state qualification replay."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
TICKETS = ("T-901", "T-902", "T-903")
REFRESH_EVENTS = {"protected_base_refreshed", "protected_base_refreshed_before_evidence"}


def run(*command: str | Path, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        [str(item) for item in command], cwd=cwd, env=env, text=True,
        capture_output=True, check=False, timeout=120,
    )
    if result.returncode:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(map(str, command))}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


class QualificationSharedStateTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        if sys.platform != "darwin" or not Path("/private/tmp").is_dir():
            self.skipTest("sealed qualification replay requires macOS")
        if os.environ.get("FACTORY_EPHEMERAL_QUALIFICATION_REPLAY") != "1":
            self.skipTest("sealed replay requires an explicit ephemeral account")
        if subprocess.run(
            ["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--"],
            check=False,
        ).returncode:
            self.skipTest("shared replay requires a committed Factory checkout")

        self.started = time.monotonic()
        self.home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        if Path.home().resolve() != self.home:
            self.fail("ephemeral replay HOME does not match the account home")
        self.account_paths = tuple(
            self.home / name for name in (".factory", ".codex", ".claude", ".cursor")
        )
        if any(path.exists() or path.is_symlink() for path in self.account_paths):
            self.fail("ephemeral replay account already contains Factory/provider state")

        self.workspace_object = tempfile.TemporaryDirectory(
            prefix="qualification-shared-state.", dir=self.home,
        )
        self.workspace = Path(self.workspace_object.name).resolve()
        suffix = self.workspace.name.rsplit(".", 1)[-1]
        self.root = Path(f"/private/tmp/nysa-sf-qualification.{suffix[:6]}")
        self.runtime_bin = Path(shutil.which("node") or "").parent
        self.root.mkdir(mode=0o700)
        self.addCleanup(self.cleanup_paths)

        self.factory_sha = run("git", "rev-parse", "HEAD", cwd=ROOT)
        self.factory_tree = run("git", "rev-parse", "HEAD^{tree}", cwd=ROOT)
        self.factory = self.workspace / "factory"
        self.installed_release = (
            self.home / f".factory/kits/releases/{self.factory_sha}"
        )
        self.project = f"shared-replay-{os.getpid()}-{suffix.lower()}"
        self.remote = self.workspace / "product.git"
        self.product = self.workspace / "product"
        self.provider_calls = self.workspace / "provider-calls.json"
        self.github_state = self.workspace / "github-state.json"

        self.make_home()
        self.make_product()
        self.make_external_fakes()
        self.pin_provider_clis()

    def cleanup_paths(self) -> None:
        self.stop_replay_processes()
        for path in (self.root, *self.account_paths):
            if not path.exists() or path.is_symlink():
                continue
            for item in sorted(path.rglob("*"), key=lambda value: len(value.parts), reverse=True):
                if not item.is_symlink():
                    item.chmod(item.stat().st_mode | stat.S_IWUSR)
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
            shutil.rmtree(path)
        self.workspace_object.cleanup()

    def stop_replay_processes(self) -> None:
        groups = self.replay_process_groups()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for group in groups:
                try:
                    os.killpg(group, sig)
                except ProcessLookupError:
                    pass
            if sig == signal.SIGTERM and groups:
                time.sleep(1)

    def replay_process_groups(self) -> set[int]:
        output = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,command="], text=True,
            capture_output=True, check=False,
        ).stdout
        groups = set()
        for line in output.splitlines():
            fields = line.strip().split(maxsplit=2)
            if len(fields) != 3:
                continue
            _, group, command = fields
            if (str(self.root) in command or self.project in command) and int(group) != os.getpgrp():
                groups.add(int(group))
        return groups

    @property
    def environment(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": f"{self.home}/.factory/bin:{self.runtime_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(self.workspace),
        }

    def sealed(self, *command: str | Path, timeout: int = 510) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            [str(item) for item in command], cwd=self.workspace,
            env=self.environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def make_home(self) -> None:
        factory = self.home / ".factory"
        release = self.installed_release
        for path in (
            factory / "accounting", factory / "bin", factory / "kits/manifests",
            factory / "kits/projects", release,
            self.home / ".codex", self.home / ".claude", self.home / ".cursor",
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        factory.chmod(0o700)
        run("git", "clone", "-q", "--no-local", str(ROOT), str(self.factory), cwd=self.workspace)
        run("git", "checkout", "-q", "--detach", self.factory_sha, cwd=self.factory)
        archive = subprocess.run(
            ["git", "-C", str(self.factory), "archive", self.factory_sha],
            capture_output=True, check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            bundle.extractall(release)
        manifest = {
            "canonical_origin": "github.com/nysa-company/software-factory",
            "created_at": "2026-08-18T00:00:00Z",
            "git_tree": self.factory_tree,
            "kit_sha": self.factory_sha,
            "schema_version": 1,
            "sealed_release_path": str(release),
        }
        path = factory / f"kits/manifests/{self.factory_sha}.json"
        path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)
        for item in (release, *release.rglob("*")):
            if not item.is_symlink():
                item.chmod(item.stat().st_mode & ~0o222)
        (factory / "global.env").write_text(
            "GLOBAL_DAILY_CAP_USD=300.000000\n"
            "CLAUDE_CODE_PINNED=2.1.223\n"
            "CODEX_PINNED=0.147.0\n"
            "CURSOR_AGENT_VERSION=2026.08.test\n"
            "FACTORY_CURSOR_FALLBACK_ENABLED=1\n",
            encoding="utf-8",
        )
        (factory / "global.env").chmod(0o600)
        (self.home / ".codex/auth.json").write_text("{}\n", encoding="utf-8")
        (self.home / ".claude/.credentials.json").write_text(
            json.dumps({
                "claudeAiOauth": {
                    "accessToken": "fixture",
                    "expiresAt": 4_102_444_800_000,
                    "refreshToken": "",
                    "refreshTokenExpiresAt": 4_102_444_800_000,
                    "scopes": ["user:inference"],
                    "subscriptionType": "team",
                }
            }) + "\n",
            encoding="utf-8",
        )
        for name in ("auth.json", "cli-config.json"):
            (self.home / ".cursor" / name).write_text("{}\n", encoding="utf-8")
        for path in (
            self.home / ".codex/auth.json",
            self.home / ".claude/.credentials.json",
            self.home / ".cursor/auth.json",
            self.home / ".cursor/cli-config.json",
        ):
            path.chmod(0o600)

    def make_product(self) -> None:
        run("git", "init", "--bare", "-q", "-b", "main", str(self.remote), cwd=self.workspace)
        (self.product / "factory/tickets").mkdir(parents=True)
        (self.product / "factory/initiatives").mkdir()
        (self.product / "app").mkdir()
        (self.product / "tests").mkdir()
        (self.product / "app/base.txt").write_text("base\n", encoding="utf-8")
        (self.product / "tests/base.txt").write_text("base\n", encoding="utf-8")
        (self.product / "factory/KIT_PIN").write_text(
            self.factory_sha + "\n", encoding="utf-8",
        )
        (self.product / "factory/initiatives/I-001.md").write_text(
            "# Shared qualification replay\n\nStatus: active\n",
            encoding="utf-8",
        )
        (self.product / "factory/model-policy.json").write_text(
            json.dumps(self.model_policy(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "300.000000",
                "capacity": 3,
                "contract_version": "2.0.0",
                "factory_sha": self.factory_sha,
                "generation": 1,
                "per_run_budget_usd": "10.000000",
                "per_ticket_budget_usd": "100.000000",
                "schema": "nysa.software-factory.qualification/v2",
                "target_done": 3,
                "tickets": list(TICKETS),
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (self.product / "factory/PROJECT.env").write_text(
            "PROJECT_NAME=shared-replay\n"
            "GH_REPO=example/product\n"
            "TICKET_BRANCH_PREFIX=ticket/\n"
            "TEST_PATHS=tests/\n"
            "PREVIEW_PROVIDER=none\n"
            "NONVISUAL_PATHS=app/,tests/\n"
            "DONE_REQUIRED_CHECKS=ci\n"
            "AUTO_MERGE_METHOD=merge\n"
            "MAX_CONCURRENT_TICKETS=3\n",
            encoding="utf-8",
        )
        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_RUN_BUDGET_USD=10.000000\n"
            "PER_TICKET_BUDGET_USD=100.000000\n"
            "PER_RUN_MAX_TURNS=10\n"
            "PER_RUN_TIMEOUT_MIN=2\n"
            "DAILY_CAP_USD=300.000000\n",
            encoding="utf-8",
        )
        (self.product / "factory/ledger.csv").write_text(
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,"
            "exit_status,run_id,provider_family,model_id,selection_reason,"
            "cost_basis,adapter_version\n",
            encoding="utf-8",
        )
        runtime = {
            "node": run("node", "--version", cwd=self.workspace),
            "npm": run("npm", "--version", cwd=self.workspace),
        }
        (self.product / "factory/certification-plan.json").write_text(
            json.dumps({
                "phases": [{
                    "artifacts": [], "command": ["true"], "depends_on": [],
                    "name": "control", "network": "denied",
                }],
                "runtime": runtime,
                "schema": "nysa.software-factory.certification-plan/v2",
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for ticket in TICKETS:
            (self.product / f"tests/{ticket}.txt").write_text(
                "pending\n", encoding="utf-8",
            )
            (self.product / f"factory/tickets/{ticket}.md").write_text(
                f"# {ticket} — shared qualification replay\n\n"
                "State: Ready\nPriority: low\nRisk class: low\nExternal: no\n"
                "Product-Decisions: frozen\nInitiative: I-001\nDepends-On: none\n"
                f"Builder ownership: app/{ticket}.txt only\n"
                f"Fixture-Seams: tests/{ticket}.txt\n"
                "Authentication-Seams: none\nProtected-Test-Conflicts: none\n\n"
                "## Description\n\nCreate the bounded replay fixture.\n\n"
                "## Acceptance criteria\n\n"
                f"1. `app/{ticket}.txt` records the ticket.\n"
                f"2. `tests/{ticket}.txt` records the test seam.\n",
                encoding="utf-8",
            )
        (self.product / ".gitignore").write_text(
            "factory/runtime-ledger.csv\nfactory/runs/\nfactory/.active-runs/\n"
            "factory/.dispatch-leases/\nfactory/.dispatch-leases.lock/\n"
            "factory/.launch.lock/\nfactory/.provider.lock/\nfactory/.ledger.lock/\n"
            "factory/operator-map.json\n",
            encoding="utf-8",
        )
        run("git", "init", "-q", "-b", "main", cwd=self.product)
        run("git", "config", "user.name", "Qualification Replay", cwd=self.product)
        run("git", "config", "user.email", "replay@example.invalid", cwd=self.product)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "Create shared qualification product", cwd=self.product)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.product)
        run("git", "push", "-qu", "origin", "main", cwd=self.product)
        run(
            "git", "remote", "set-url", "origin",
            "git@github.com:example/product.git", cwd=self.product,
        )

        self.operator_seed = self.workspace / "operator-map-seed.json"
        self.operator_seed.write_text(json.dumps({
            "_config": {
                "labels": {}, "states": {}, "team_id": "team-id",
                "team_key": "SF", "template_id": "template-id",
            },
            "_sync": {},
            "initiatives": {"I-001": {"project_id": "project-id"}},
            "tickets": {},
        }, sort_keys=True) + "\n", encoding="utf-8")
        self.operator_seed.chmod(0o600)

    def make_external_fakes(self) -> None:
        vendor = self.workspace / "vendor"
        vendor.mkdir(mode=0o700)
        for name in ("agent", "claude", "codex"):
            provider = vendor / name
            provider.write_text(self.provider_source(name), encoding="utf-8")
            provider.chmod(0o700)
        host = vendor / "codex-code-mode-host"
        host.write_text("#!/bin/sh\nprintf '%s\\n' --listen\n", encoding="utf-8")
        host.chmod(0o700)
        self.vendor = vendor

        ssh = self.home / ".factory/bin/ssh"
        ssh.write_text(
            "#!/bin/sh\nset -eu\n"
            "case \"$*\" in\n"
            f"  *git-upload-pack*) exec git-upload-pack '{self.remote}' ;;\n"
            f"  *git-receive-pack*) exec git-receive-pack '{self.remote}' ;;\n"
            "esac\nexit 2\n",
            encoding="utf-8",
        )
        ssh.chmod(0o700)
        for name in ("curl", "scp", "wget"):
            blocked = self.home / f".factory/bin/{name}"
            blocked.write_text(
                "#!/bin/sh\necho 'external command blocked' >&2\nexit 97\n",
                encoding="utf-8",
            )
            blocked.chmod(0o700)

        self.github_state.write_text(
            '{"calls":[],"next":1,"prs":[]}\n', encoding="utf-8",
        )
        self.github_state.chmod(0o600)
        gh = self.home / ".factory/bin/gh"
        gh.write_text(f'''#!/usr/bin/env python3
import base64, fcntl, json, pathlib, subprocess, sys, tempfile
state=pathlib.Path({str(self.github_state)!r})
remote=pathlib.Path({str(self.remote)!r})
args=sys.argv[1:]
endpoint=next((arg for arg in args[1:] if arg.startswith("repos/")), "")
with state.open("r+",encoding="utf-8") as stream:
    fcntl.flock(stream,fcntl.LOCK_EX)
    value=json.load(stream)
    value["calls"].append(args)
    def save():
        stream.seek(0); stream.truncate(); json.dump(value,stream,sort_keys=True)
        stream.write("\\n"); stream.flush()
    def option(name, default=""):
        return args[args.index(name)+1] if name in args else default
    def head(branch):
        return subprocess.check_output(
            ["git","--git-dir",str(remote),"rev-parse",f"refs/heads/{{branch}}"],
            text=True,
        ).strip()
    def item(pr):
        return {{**pr,"headRefOid":head(pr["headRefName"])}} if pr["state"] == "OPEN" else dict(pr)
    if args[:1] == ["--version"]:
        print("gh version 2.78.0")
    elif args[:2] == ["auth","status"]:
        pass
    elif args[:2] == ["pr","list"]:
        wanted=option("--state","open").upper()
        found=[item(pr) for pr in value["prs"] if
               (wanted == "ALL" or pr["state"] == wanted.upper()) and
               (not option("--head") or pr["headRefName"] == option("--head")) and
               (not option("--base") or pr["baseRefName"] == option("--base"))]
        print(json.dumps(found))
    elif args[:2] == ["pr","create"]:
        branch=option("--head")
        number=value["next"]; value["next"]+=1
        value["prs"].append({{
            "autoMergeRequest":None,"baseRefName":option("--base"),
            "createdAt":"2026-08-18T00:00:00Z",
            "headRefName":branch,"headRefOid":head(branch),"isDraft":False,"mergeCommit":None,
            "mergeStateStatus":"CLEAN","mergedAt":None,"number":number,
            "state":"OPEN","url":f"https://github.com/example/product/pull/{{number}}",
        }})
        save(); print(f"https://github.com/example/product/pull/{{number}}")
    elif args[:2] == ["pr","checks"]:
        print(json.dumps([{{"bucket":"pass","link":"https://example.invalid/ci",
                           "name":"ci","state":"SUCCESS"}}]))
    elif args[:2] == ["pr","ready"]:
        number=int(args[2]); pr=next(pr for pr in value["prs"] if pr["number"] == number)
        pr["isDraft"]="--undo" in args; save()
    elif args[:2] == ["pr","view"]:
        number=int(args[2]); print(json.dumps(item(next(
            pr for pr in value["prs"] if pr["number"] == number
        ))))
    elif args[:2] == ["pr","merge"]:
        number=int(args[2]); pr=next(pr for pr in value["prs"] if pr["number"] == number)
        if "--disable-auto" in args:
            pr["autoMergeRequest"]=None; save()
        elif pr["state"] != "MERGED":
            pr["headRefOid"]=head(pr["headRefName"])
            with tempfile.TemporaryDirectory(prefix="qualification-gh.") as temporary:
                subprocess.run(["git","clone","-q",str(remote),temporary],check=True)
                subprocess.run(["git","-C",temporary,"config","user.name","Qualification GitHub"],check=True)
                subprocess.run(["git","-C",temporary,"config","user.email","github@example.invalid"],check=True)
                subprocess.run(["git","-C",temporary,"merge","--no-ff","-m",f"Merge PR #{{number}}",f"origin/{{pr['headRefName']}}"],check=True,capture_output=True)
                subprocess.run(["git","-C",temporary,"push","-q","origin","main"],check=True)
                merged=subprocess.check_output(["git","-C",temporary,"rev-parse","HEAD"],text=True).strip()
            pr.update(autoMergeRequest={{"mergeMethod":"MERGE"}},mergeCommit={{"oid":merged}},
                      mergedAt="2026-08-18T00:01:00Z",state="MERGED")
            save()
    elif args[:2] == ["pr","close"]:
        number=int(args[2]); pr=next(pr for pr in value["prs"] if pr["number"] == number)
        pr.update(autoMergeRequest=None,state="CLOSED"); save()
    elif args[:1] == ["api"] and endpoint.startswith("repos/example/product/rules/branches/main"):
        print(json.dumps([[{{"type":"required_status_checks","parameters":{{
            "required_status_checks":[{{"context":"ci","integration_id":15368}}]
        }}}}]]))
    elif args[:1] == ["api"] and "/pulls/" in endpoint and endpoint.split("?",1)[0].endswith("/files"):
        number=int(endpoint.split("/pulls/",1)[1].split("/",1)[0])
        pr=next(pr for pr in value["prs"] if pr["number"] == number)
        diff=subprocess.check_output([
            "git","--git-dir",str(remote),"diff","--name-status",
            f"refs/heads/main...refs/heads/{{pr['headRefName']}}",
        ],text=True)
        for line in diff.splitlines():
            status,path=line.split("\\t",1)
            row={{"filename":path,"status":{{"A":"added","D":"removed"}}.get(status,"modified")}}
            print(base64.b64encode(json.dumps(row).encode()).decode())
    elif args[:1] == ["api"] and "/commits/" in endpoint and endpoint.endswith("/status"):
        print(json.dumps({{"statuses":[]}}))
    elif args[:1] == ["api"] and "/check-runs" in endpoint:
        print(json.dumps({{"check_runs":[{{
            "conclusion":"success","name":"ci","status":"completed"
        }}]}}))
    else:
        save(); print("unsupported fake gh command",args,file=sys.stderr); raise SystemExit(2)
    save()
''', encoding="utf-8")
        gh.chmod(0o700)

    @staticmethod
    def model_policy() -> dict[str, object]:
        roles = {
            "planner": ("cursor-gpt-5.6-sol-high", "codex-gpt-5.6-sol", "high"),
            "builder": ("cursor-gpt-5.6-sol-high", "codex-gpt-5.6-terra", "high"),
            "narrator": ("cursor-gpt-5.6-sol-high", "codex-gpt-5.6-terra", "high"),
            "reviewer": (
                "cursor-claude-sonnet-5-thinking-high", "claude-sonnet", "high",
            ),
            "spec-linter": (
                "cursor-claude-fable-5-thinking-medium", "claude-fable", "medium",
            ),
            "test-author": (
                "cursor-claude-fable-5-thinking-medium", "claude-fable", "medium",
            ),
        }
        return {
            "checking_family": "anthropic",
            "production_family": "openai",
            "roles": {
                role: {
                    "effort": values[2],
                    "primary_route_id": values[0],
                    "secondary_route_id": values[1],
                }
                for role, values in roles.items()
            },
            "schema": "factory-model-policy/v1",
            "version": 1,
        }

    def provider_source(self, provider_name: str) -> str:
        calls = str(self.provider_calls)
        return f'''#!/usr/bin/env python3
import fcntl, json, os, pathlib, subprocess, sys
name={provider_name!r}
args=sys.argv[1:]
if name == "codex" and args[:1] == ["--version"]:
    print("codex-cli 0.147.0"); raise SystemExit(0)
if name == "codex" and args[:2] == ["exec", "--help"]:
    print("--json --model"); raise SystemExit(0)
if name == "codex" and args[:2] == ["login", "status"]:
    raise SystemExit(0)
if name == "claude" and args[:1] == ["--version"]:
    print("2.1.223 (Claude Code)"); raise SystemExit(0)
if name == "claude" and args[:1] == ["--help"]:
    print("--max-budget-usd --output-format --append-system-prompt --model --effort")
    raise SystemExit(0)
if name == "claude" and args[:2] == ["auth", "status"]:
    raise SystemExit(0)
if name == "agent" and args[:1] == ["--version"]:
    print("agent 2026.08.test"); raise SystemExit(0)
if name == "agent" and args[:1] == ["--help"]:
    print("--print --output-format --workspace --model --force --trust")
    raise SystemExit(0)
if name == "agent" and args[:1] == ["status"]:
    print('{{"authenticated":true}}'); raise SystemExit(0)
if name == "agent" and args[:1] == ["models"]:
    print("gpt-5.6-sol-high claude-fable-5-thinking-medium claude-sonnet-5-thinking-high")
    raise SystemExit(0)

work=pathlib.Path.cwd()
branch=subprocess.check_output(["git","-C",str(work),"symbolic-ref","--short","HEAD"],text=True).strip()
ticket=branch.rsplit("/",1)[-1]
role=os.environ["FACTORY_ROLE"]
path=pathlib.Path({calls!r})
path.touch(mode=0o600,exist_ok=True)
with path.open("r+",encoding="utf-8") as stream:
    fcntl.flock(stream,fcntl.LOCK_EX)
    raw=stream.read().strip()
    value=json.loads(raw) if raw else {{}}
    key=f"{{ticket}}:{{role}}:{{name}}"
    value[key]=value.get(key,0)+1
    stream.seek(0); stream.truncate()
    json.dump(value,stream,sort_keys=True); stream.write("\\n"); stream.flush(); os.fsync(stream.fileno())
ordinal=value[key]
text=f"{{ticket}} {{role}} complete"
if role == "reviewer":
    text="## Verdict: APPROVE\\n\\nNo findings."
else:
    if role == "planner":
        target=work/"factory"/"tickets"/f"{{ticket}}.md"
        target.write_text(target.read_text()+"\\nPLAN: fixture\\n")
    elif role == "spec-linter":
        target=work/"factory"/"tickets"/f"{{ticket}}.md"
        target.write_text(target.read_text()+"\\nSPEC-LINT: PASS\\n")
    elif role == "test-author":
        target=work/"tests"/f"{{ticket}}.txt"; target.write_text("test\\n")
    elif role == "builder":
        target=work/"app"/f"{{ticket}}.txt"; target.write_text("built\\n")
    elif role == "narrator":
        target=work/"factory"/"tickets"/f"{{ticket}}-bundle.md"
        target.write_text("# What this does\\nDone.\\n# Preview\\nNot applicable — backend-only contract.\\n# Screenshots\\nNot applicable — backend-only contract.\\n# Acceptance criteria\\nPass.\\n# Risk\\nLow.\\n# Cost\\nFixture.\\n# Rollback\\nRevert.\\nApprove to merge?\\n")
    subprocess.run(["git","-C",str(work),"add","."],check=True)
    subprocess.run(["git","-C",str(work),"-c","user.name=Replay Provider","-c","user.email=replay@example.invalid","commit","-qm",f"{{ticket}}: {{role}} fixture"],check=True)
failed = ticket == "T-902" and role == "builder" and name == "agent" and ordinal == 1
if name == "agent":
    model=args[args.index("--model")+1]
    reports={{
        "gpt-5.6-sol-high":"GPT-5.6 Sol 272K High",
        "claude-fable-5-thinking-medium":"Fable 5 300K Medium",
        "claude-sonnet-5-thinking-high":"Sonnet 5 300K High",
    }}
    print(json.dumps({{"type":"system","subtype":"init","model":reports[model]}}))
    print(json.dumps({{"type":"assistant","message":{{"content":text}}}}))
    print(json.dumps({{"type":"result","subtype":"success","result":text}}))
elif name == "claude":
    print(json.dumps({{"type":"result","subtype":"success","is_error":False,"num_turns":1,"total_cost_usd":0.01,"result":text}}))
else:
    print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":text}}}}))
    print(json.dumps({{"input_tokens":10,"output_tokens":10}}))
raise SystemExit(1 if failed else 0)
'''

    def pin_provider_clis(self) -> None:
        release = self.installed_release
        helper = release / "scripts/owner-provider-cli-pin.py"
        qualifications = self.workspace / "qualifications"
        qualifications.mkdir(mode=0o700)
        environment = {
            **self.environment,
            "FACTORY_PROVIDER_CLI_PIN_TEST_QUALIFICATION_ROOT": str(qualifications),
            "FACTORY_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
        }
        base = (
            "python3", "-I", "-S", helper,
            "--kits-root", self.home / ".factory/kits",
            "--sha", self.factory_sha, "--tree", self.factory_tree,
            "--release", release,
        )
        plan = json.loads(run(
            *base, "plan", "--claude-bin", self.vendor / "claude",
            "--codex-bin", self.vendor / "codex",
            "--cursor-bin", self.vendor / "agent",
            "--operator-id", "qualification-replay", cwd=self.workspace,
            env=environment,
        ))
        run(
            *base, "apply", "--claude-bin", self.vendor / "claude",
            "--codex-bin", self.vendor / "codex",
            "--cursor-bin", self.vendor / "agent",
            "--operator-id", "qualification-replay",
            "--approve-hash", plan["approval_sha256"], cwd=self.workspace,
            env=environment,
        )

    def approve_waiting(self, release: Path) -> int:
        controller = self.home / f".factory/qualification/{self.project}/controller"
        operator_map = (
            self.home / f".factory/qualification/{self.project}/operator/operator-map.json"
        )
        approved = 0
        for path in sorted((controller / "claims").glob("T-*.json")):
            claim = json.loads(path.read_text(encoding="utf-8"))
            worktree = Path(claim["worktree"])
            ticket = claim["ticket"]
            text = (worktree / f"factory/tickets/{ticket}.md").read_text(encoding="utf-8")
            if "State: Awaiting Approval" not in text:
                continue
            result = subprocess.run(
                [
                    sys.executable, "-I", "-S", str(release / "scripts/operator-cli.py"),
                    "--product", str(worktree), "--state-dir", str(controller),
                    "approve", "--ticket", ticket,
                ],
                cwd=self.workspace,
                env={
                    **self.environment,
                    "FACTORY_OPERATOR_AUDIT_COMMIT": "0",
                    "FACTORY_OPERATOR_MAP": str(operator_map),
                    "FACTORY_RELEASE_CONTRACT_VERSION": "2.0.0",
                },
                text=True, capture_output=True, check=False, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            approved += 1
        return approved

    def test_shared_cohort_runs_through_real_sealed_qualification(self) -> None:
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/qualification-environment.py"),
                "--factory-root", str(self.factory),
                "--product-root", str(self.product),
                "--project", self.project,
                "--root", str(self.root),
                "--runtime-bin", str(self.runtime_bin),
                "--global-env", str(self.home / ".factory/global.env"),
                "--operator-map-seed", str(self.operator_seed),
            ],
            cwd=self.workspace, env=self.environment, text=True,
            capture_output=True, check=False, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(value["factory_sha"], self.factory_sha)
        launcher = Path(value["launcher"])
        self.assertTrue(launcher.is_file())
        approved = 0
        blocked_waves = 0
        restarts = 0
        events_dir = self.home / f".factory/qualification/{self.project}/controller/events"
        for _ in range(7):
            prior_events = set(events_dir.glob("*.json"))
            launched = self.sealed(
                str(launcher), self.project, "qualification-run", "--json",
                timeout=max(1, int(530 - (time.monotonic() - self.started))),
            )
            self.assertIn(launched.returncode, (0, 3), launched.stdout + launched.stderr)
            replay = json.loads(launched.stdout)
            restarts += replay["restarts"]
            if replay["status"] == "green":
                self.assertEqual(launched.returncode, 0)
                break
            self.assertIn(
                replay["status"], {"blocked", "waiting"},
                json.dumps(replay, sort_keys=True),
            )
            self.assertEqual(launched.returncode, 3)
            if replay["status"] == "blocked":
                blocked_waves += 1
                self.assertEqual(
                    {item["ticket"]: item["status"] for item in replay["controller"]["results"]},
                    {"T-901": "waiting", "T-902": "blocked", "T-903": "waiting"},
                )
            newly_approved = self.approve_waiting(launcher.parent.parent)
            if newly_approved == 0:
                new_events = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in events_dir.glob("*.json") if path not in prior_events
                ]
                self.assertEqual(replay["status"], "waiting")
                self.assertTrue(
                    any(event.get("event") in REFRESH_EVENTS for event in new_events),
                    json.dumps(replay, sort_keys=True),
                )
            approved += newly_approved
        else:
            self.fail("shared qualification did not converge")
        self.assertEqual(replay["status"], "green", json.dumps(replay, sort_keys=True))
        self.assertEqual(blocked_waves, 1)
        self.assertEqual(restarts, 1)
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(events_dir.glob("*.json"))
        ]
        fallback = [event for event in events if event.get("event") == "provider_fallback"]
        self.assertEqual([event["ticket"] for event in fallback], ["T-902"])
        refreshes = [
            event for event in events
            if event.get("event") in REFRESH_EVENTS
        ]
        self.assertEqual(len(refreshes), 3)
        self.assertEqual(
            approved,
            3 + sum(event.get("event") == "protected_base_refreshed" for event in events),
        )
        calls = json.loads(self.provider_calls.read_text(encoding="utf-8"))
        expected = {
            f"{ticket}:{role}:agent": 1
            for ticket in TICKETS
            for role in ("planner", "spec-linter", "test-author", "builder")
        }
        expected["T-902:builder:codex"] = 1
        for ticket in TICKETS:
            refresh_count = sum(
                event.get("ticket") == ticket
                and event.get("event") in REFRESH_EVENTS
                for event in events
            )
            expected[f"{ticket}:reviewer:agent"] = 1 + refresh_count
            expected[f"{ticket}:narrator:agent"] = 1 + refresh_count
        self.assertEqual(calls, expected)
        self.assertEqual(
            {item["ticket"] for item in replay["report"]["tickets"]}, set(TICKETS)
        )
        self.assertEqual(self.replay_process_groups(), set())
        self.assertLess(time.monotonic() - self.started, 540)


if __name__ == "__main__":
    unittest.main()
