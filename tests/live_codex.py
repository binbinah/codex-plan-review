#!/usr/bin/env python3
"""Opt-in real Codex Plan -> direct body review -> native worker execution test.

Uses a private temporary CODEX_HOME and workspace. No global installation or
configuration is modified. Provider/model settings are inherited, never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/plan-review/scripts"))
from plan_review.context import redact  # noqa: E402
from plan_review.state import Store, digest  # noqa: E402


def toml_value(value):
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{json.dumps(k)}={toml_value(v)}" for k, v in value.items() if v is not None
            )
            + "}"
        )
    raise ValueError(f"Unsupported config type: {type(value).__name__}")


def checked(argv, env, cwd=None):
    result = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} {argv[1]} failed: exit {result.returncode}")
    return result.stdout


class AppServer:
    def __init__(self, env):
        self.proc = subprocess.Popen(
            ["codex", "app-server", "--stdio"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.messages = queue.Queue()
        self.events = []
        self.next_id = 0
        self.diagnostics = ""
        threading.Thread(target=self._read, daemon=True).start()
        # Consume diagnostics without publishing provider configuration or prompts.
        threading.Thread(target=self._diagnostics, daemon=True).start()

    def _diagnostics(self):
        for line in self.proc.stderr:
            self.diagnostics = (self.diagnostics + redact(line))[-12000:]

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.messages.put({"fatal": "app-server stdout closed"})

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def receive(self, timeout):
        message = self.messages.get(timeout=timeout)
        if "fatal" in message:
            raise RuntimeError(message["fatal"])
        if "method" in message and "id" not in message:
            self.events.append(message)
        if "method" in message and "id" in message:
            # Tests must not approve arbitrary permissions or answer missing requirements.
            self.send(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32603,
                        "message": "Live test does not grant extra permissions",
                    },
                }
            )
        return message

    def request(self, method, params, timeout=60):
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.receive(max(0.1, deadline - time.monotonic()))
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message["result"]
        raise TimeoutError(method)

    def turn(self, thread, model, mode, prompt, timeout=420, sandbox=None):
        result = self.request(
            "turn/start",
            {
                "threadId": thread,
                "input": [{"type": "text", "text": prompt}],
                **({"sandboxPolicy": sandbox} if sandbox else {}),
                "collaborationMode": {
                    "mode": mode,
                    "settings": {
                        "model": model,
                        "developer_instructions": None,
                    },
                },
            },
        )
        turn_id = result["turn"]["id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.receive(max(0.1, deadline - time.monotonic()))
            if message.get("method") == "turn/completed":
                params = message["params"]
                if params.get("threadId") == thread and params["turn"]["id"] == turn_id:
                    if params["turn"].get("error"):
                        raise RuntimeError(f"Turn failed: {params['turn']['error']}")
                    if params["turn"].get("status") != "completed":
                        raise RuntimeError(f"Turn status: {params['turn'].get('status')}")
                    return params
        raise TimeoutError("turn/completed")

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()


def run(inventory_only=False):
    source_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    config = tomllib.loads((source_home / "config.toml").read_text())
    with tempfile.TemporaryDirectory(prefix="codex-plan-review-live-") as temp:
        root = Path(temp)
        home, workspace, data = root / "home", root / "workspace", root / "data"
        home.mkdir(mode=0o700)
        workspace.mkdir()
        inherited = {
            k: config[k]
            for k in (
                "model",
                "model_provider",
                "model_reasoning_effort",
                "model_providers",
                "model_catalog_json",
                "cli_auth_credentials_store",
            )
            if k in config
        }
        inherited.update(
            approval_policy="never",
            sandbox_mode="workspace-write",
            web_search="disabled",
            features={"hooks": True, "plugins": True, "multi_agent": True, "apps": False},
        )
        cfg = home / "config.toml"
        cfg.write_text("\n".join(f"{k}={toml_value(v)}" for k, v in inherited.items()) + "\n")
        cfg.chmod(0o600)
        if (source_home / "auth.json").is_file():
            shutil.copyfile(source_home / "auth.json", home / "auth.json")
            (home / "auth.json").chmod(0o600)
        env = {
            **os.environ,
            "CODEX_HOME": str(home),
            "PLAN_REVIEW_DATA_DIR": str(data),
            "PLAN_REVIEW_TIMEOUT_SECONDS": "240",
            "PLAN_REVIEW_MAX_ROUNDS": "3",
        }
        checked(["git", "init", "--initial-branch=main", str(workspace)], env)
        (workspace / "AGENTS.md").write_text(
            "Use the installed plan-review workflow. Only result.json may be created.\n"
            "When asked to implement, spawn one worker and verify its returned artifact.\n"
        )
        checked(["git", "-C", str(workspace), "add", "AGENTS.md"], env)
        checked(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "user.name=Live Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            env,
        )
        print("LIVE phase=plugin_install", flush=True)
        checked(["codex", "plugin", "marketplace", "add", str(ROOT), "--json"], env)
        checked(["codex", "plugin", "add", "plan-review@codex-plan-review", "--json"], env)
        installed = json.loads(checked(["codex", "plugin", "list", "--json"], env))
        server = AppServer(env)
        try:
            server.request(
                "initialize",
                {
                    "clientInfo": {"name": "plan-review-live-test", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            server.send({"method": "initialized"})
            hook_inventory = server.request("hooks/list", {"cwds": [str(workspace)]})
            (ROOT / "artifacts").mkdir(exist_ok=True)
            (ROOT / "artifacts/private-hook-inventory.json").write_text(
                json.dumps(hook_inventory, ensure_ascii=False, indent=2)
            )
            if inventory_only:
                metadata = {
                    "hooks": hook_inventory,
                    "features": tomllib.loads(cfg.read_text()).get("features"),
                    "plugins": tomllib.loads(cfg.read_text()).get("plugins"),
                    "manifests": {
                        str(p.relative_to(home)): json.loads(p.read_text())
                        for p in (home / "plugins/cache").rglob("plugin.json")
                    },
                }
                return metadata
            discovered = hook_inventory["data"][0]["hooks"]
            if len(discovered) != 6:
                raise AssertionError("Installed plugin did not expose all six hooks")
            # Trust only this test-owned plugin, in the isolated test config.
            server.request(
                "config/value/write",
                {
                    "keyPath": "hooks.state",
                    "mergeStrategy": "upsert",
                    "filePath": str(cfg),
                    "value": {
                        hook["key"]: {"trusted_hash": hook["currentHash"]} for hook in discovered
                    },
                },
            )
            trusted = server.request("hooks/list", {"cwds": [str(workspace)]})
            if any(hook["trustStatus"] != "trusted" for hook in trusted["data"][0]["hooks"]):
                raise AssertionError("Test-owned hooks were not trusted")
            thread = server.request(
                "thread/start",
                {
                    "cwd": str(workspace),
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                    "ephemeral": False,
                },
            )
            thread_id = thread["thread"]["id"]
            model = thread["model"]
            print("LIVE phase=native_plan", flush=True)
            server.turn(
                thread_id,
                model,
                "plan",
                "制定一个完整实施计划：由一个 worker 创建 result.json，内容严格为 "
                '{"task":"mechanical-probe","items":[1,2,3],"sum":6}。'
                "主会话亲自读取文件并用 Python 断言完整内容以及 items 求和等于 sum。"
                "这是隔离测试工作区，已授权该本地实现。当前只制定计划，不创建文件。"
                "所有细节已给定，不需要澄清。使用已安装的 plan-review，"
                "在输出最终计划之前主动提交完整正文接受独立红队评审；"
                "通过后原样输出 proposed_plan。",
            )
            store = Store(data, thread_id, str(workspace))
            with store.locked():
                state = store.read()
            if state.get("status") != "approved":
                diagnostic = {
                    "installed": installed,
                    "state_files": [
                        {"name": p.name, "state": json.loads(p.read_text())}
                        for p in data.glob("*.json")
                    ],
                    "events": server.events,
                    "diagnostics": server.diagnostics,
                }
                (ROOT / "artifacts").mkdir(exist_ok=True)
                (ROOT / "artifacts/private-live-diagnostic.json").write_text(
                    redact(json.dumps(diagnostic, ensure_ascii=False, indent=2))
                )
                raise AssertionError(
                    f"Native Plan was not approved: {state.get('status', 'NO_STATE')}"
                )
            if (workspace / "result.json").exists():
                raise AssertionError("Implementation happened during Plan/review")
            final_plans = [
                event["params"]["item"]["text"]
                for event in server.events
                if event.get("method") == "item/completed"
                and event["params"]["item"].get("type") == "plan"
            ]
            if not final_plans or digest(final_plans[-1].strip()) != state["plan_hash"]:
                raise AssertionError("Rendered final Plan differs from the reviewed body")
            print("LIVE phase=approved_before_execution", flush=True)
            # The test client performs the normal user-selected transition to implementation.
            server.turn(
                thread_id,
                model,
                "default",
                "执行已通过评审的计划。必须实际派发一个 worker 创建 result.json，"
                "等待它回传，再由主会话亲自读取并运行 Python 验收。不要自己代替 worker 写文件。",
                sandbox={"type": "workspaceWrite", "writableRoots": [str(workspace)]},
            )
            artifact = json.loads((workspace / "result.json").read_text())
            expected = {"task": "mechanical-probe", "items": [1, 2, 3], "sum": 6}
            if artifact != expected or sum(artifact["items"]) != artifact["sum"]:
                raise AssertionError("Worker artifact failed independent verification")
            with store.locked():
                final = store.read()
            hooks_seen = [
                event["params"]["run"]
                for event in server.events
                if event.get("method") == "hook/completed"
            ]
            events = [run["eventName"] for run in hooks_seen]
            if "subagentstop" not in {name.lower() for name in events}:
                raise AssertionError("No native SubagentStop was observed")
            if not final.get("worker_reports"):
                raise AssertionError("Worker return was not recorded by installed plugin")
            result = {
                "codex_version": checked(["codex", "--version"], env).strip(),
                "marketplace_install_verified": bool(installed),
                "native_plan_review": state["result"]["verdict"],
                "plan_review_rounds": state["rounds"],
                "rendered_plan_matches_reviewed_body": True,
                "plan_sandbox": "read-only",
                "no_implementation_before_approval": True,
                "native_worker_return_observed": True,
                "worker_reports": len(final["worker_reports"]),
                "worker_agent_types": [report["agent_type"] for report in final["worker_reports"]],
                "artifact_verified": artifact,
                "completed_hook_events": events,
                "failed_hooks": [
                    run["eventName"]
                    for run in hooks_seen
                    if run["status"] not in {"completed", "succeeded"}
                ],
                "global_config_modified": False,
            }
            print("LIVE phase=worker_verified", flush=True)
            return result
        finally:
            server.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()
    result = run(args.inventory_only)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
