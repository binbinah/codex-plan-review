"""Explicit review, diagnostics, and opt-in recovery commands."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .context import redact
from .hooks import handle, normalize_plan, read_payload, review_plan
from .state import Store, data_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex Plan 红队评审")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("hook")
    commands.add_parser("doctor")
    submit = commands.add_parser("submit")
    submit.add_argument("--stdin", action="store_true", required=True)
    for name in ("review", "status", "retry", "reset"):
        child = commands.add_parser(name)
        child.add_argument("--session", required=True)
        child.add_argument("--cwd", default=str(Path.cwd()))
        child.add_argument("--data-dir", type=Path)
        if name == "review":
            source = child.add_mutually_exclusive_group(required=True)
            source.add_argument("--plan", type=Path)
            source.add_argument("--stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            raise ValueError(
                "submit 需要已安装并获信任的 PreToolUse hook；未运行红队。"
                "独立调用请使用 review --stdin --session <id> --cwd <repo>。"
            )
        if args.command == "hook":
            result = handle(read_payload(sys.stdin.read(1000001)))
        elif args.command == "doctor":
            codex = shutil.which("codex")
            version = None
            if codex:
                version = subprocess.run(
                    [codex, "--version"], capture_output=True, text=True, timeout=5
                ).stdout.strip()
            result = {
                "plugin_version": __version__,
                "python": sys.version.split()[0],
                "codex": version,
                "state_directory": str(data_root()),
                "supported_platform": sys.platform in {"darwin", "linux"},
            }
        else:
            store = Store(args.data_dir or data_root(), args.session, args.cwd)
            if args.command == "reset":
                with store.locked():
                    store.write({"status": "idle", "reset_by": "explicit_cli"})
                result = {"status": "idle", "notice": "本计划评审已取消；不改变 Codex 权限或授权。"}
            elif args.command == "status":
                with store.locked():
                    state = store.read()
                result = {k: v for k, v in state.items() if k not in {"plan", "requests"}}
            else:
                if args.command == "retry":
                    with store.locked():
                        state = store.read()
                        plan = state.get("plan")
                        if not plan:
                            raise ValueError("没有待重试的计划")
                        state.update(status="review_failed", rounds=0)
                        state.pop("review_token", None)
                        state.pop("deadline", None)
                        store.write(state)
                else:
                    plan = (
                        args.plan.read_text(encoding="utf-8")
                        if args.plan
                        else sys.stdin.read(200001)
                    )
                result = review_plan(store, Path(args.cwd), normalize_plan(plan))
        print(json.dumps(result, ensure_ascii=False))
        if args.command in {"review", "retry"}:
            with store.locked():
                return 0 if store.read().get("status") == "approved" else 3
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"plan-review: {redact(str(exc))[:800]}", file=sys.stderr)
        # Codex explicitly supports exit 2 as a blocking hook error.
        return 2
