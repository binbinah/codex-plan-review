"""Codex lifecycle adapters; technical approval preserves native authorization."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from . import reviewer
from .context import bundle, redact
from .policy import read_only_tool
from .state import StateError, Store, data_root, digest
from .submission import output_command, parse_submission, recovery_command, submit_command

WORKFLOW = """Before finalizing a native Plan, submit its complete Markdown body
to the installed plan-review command using a literal single-quoted heredoc.
The PreToolUse hook receives that exact body, independently reviews it on the host,
and returns JSON through the requested command's stdout. No session id is needed.
After verdict=approve, emit the identical plan in proposed_plan, with no surrounding prose.
If concerns/reject, revise or rebut with evidence and submit the complete body again.
If the engine fails or reaches its budget, report the blocker and stop implementation.
Do not emit proposed_plan for questions, status updates, or ordinary non-Plan work.
Technical approval preserves the host's normal Plan-to-execution decision and existing
user authorization. It does not grant permission for new external actions.
Delegate mechanical work only when the user or applicable skill/project instructions
request it. Specify files, required behavior, dependencies, and acceptance commands.
Verify returned artifacts and command results before accepting a worker's claim.
Specify each actual target project with --project <directory>, especially from a
multi-repository workspace. Add --evidence <file> for the key existing source files
already inspected; paths must belong to those projects. Do not redo kickoff in the reviewer.
"""

WORKER = """Execute only the parent's explicit assignment. Keep edits within the
assigned files and preserve other agents' work. Do not choose new architecture,
expand scope, or perform unauthorized external actions. If the assignment requires
an unresolved decision or missing information, return that blocker to the parent.
Report changed paths, executed validation commands, exit codes, and remaining gaps.
"""


def limits() -> tuple[int, int]:
    try:
        timeout = int(os.environ.get("PLAN_REVIEW_TIMEOUT_SECONDS", str(reviewer.DEFAULT_TIMEOUT)))
        rounds = int(os.environ.get("PLAN_REVIEW_MAX_ROUNDS", "3"))
    except ValueError as exc:
        raise StateError("PLAN_REVIEW_TIMEOUT_SECONDS/MAX_ROUNDS 必须是整数") from exc
    if not 1 <= timeout <= reviewer.MAX_TIMEOUT or not 1 <= rounds <= 10:
        raise StateError(f"评审超时须为 1–{reviewer.MAX_TIMEOUT} 秒，轮次须为 1–10")
    return timeout, rounds


def deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def paused(reason: str) -> dict:
    return {"continue": False, "systemMessage": reason}


def normalize_plan(plan: str) -> str:
    plan = plan.strip()
    if not plan or "<proposed_plan>" in plan or "</proposed_plan>" in plan:
        raise StateError("正式计划为空或包含嵌套标签；请重新提交完整计划正文")
    if len(plan.encode("utf-8")) > 200000:
        raise StateError("计划超过 200 KB；请压缩重复内容后重新提交")
    return plan


def extract_plan(message: str) -> str | None:
    match = re.fullmatch(r"\s*<proposed_plan>\s*(.*?)\s*</proposed_plan>\s*", message, re.S)
    if not match:
        return None
    return normalize_plan(match.group(1))


def describe(result: dict) -> str:
    lines = [f"红队评审：{result['verdict'].upper()}\n{result['summary']}"]
    for finding in result["findings"]:
        verified = "已核实" if finding["verified"] else "待核实"
        lines.append(
            f"[{finding['severity']}/{verified}] {finding['evidence']}\n"
            f"影响：{finding['impact']}\n修复：{finding['fix']}"
        )
    return "\n\n".join(lines)[:10000]


def review_plan(
    store: Store,
    cwd: Path,
    plan: str,
    review_fn: Callable = reviewer.run,
    scope: dict | None = None,
) -> dict:
    timeout, max_rounds = limits()
    with store.locked():
        state = store.read()
        if scope is None and state.get("plan_hash") == digest(plan):
            scope = state.get("review_scope")
        try:
            context = bundle(cwd, plan, state.get("requests", []), state.get("history", []), scope)
        except (OSError, ValueError) as exc:
            if state.get("status") in {"approved", "executing"}:
                state.update(rounds=0, history=[], worker_reports=[])
            state.update(
                status="needs_revision",
                plan=redact(plan),
                plan_hash=digest(plan),
                stored_plan_hash=digest(redact(plan)),
                context_digest=state.get("context_digest", digest("")),
                rounds=state.get("rounds", 0),
                error=redact(str(exc))[:1000],
            )
            for key in ("result", "review_token", "deadline", "progress"):
                state.pop(key, None)
            store.write(state)
            raise
        plan_hash = digest(plan)
        basis = context["repository"]["basis_hash"]
        if state.get("plan_hash") == plan_hash and state.get("context_digest") == basis:
            if state.get("status") in {"approved", "executing"}:
                return {"systemMessage": "红队已通过该计划；继续原有 Plan→执行流程。"}
            if state.get("status") == "reviewing" and state.get("deadline", 0) > time.time():
                return paused("该计划已有红队评审正在运行；本次未启动重复进程。")
        if state.get("status") in {"approved", "executing"}:
            # A new plan after a completed cycle gets a new budget and review history.
            state.update(rounds=0, history=[], worker_reports=[])
            context["previous_reviews"] = []
        if state.get("rounds", 0) >= max_rounds:
            return paused(
                f"红队评审已达 {max_rounds} 轮，实施仍未放行。请向用户说明剩余问题；"
                "用户要求继续评审时可显式 retry，取消本计划时可显式 reset。"
            )
        token = uuid.uuid4().hex
        state.pop("result", None)
        state.pop("diagnostics", None)
        state.pop("progress", None)
        state.pop("review_metrics", None)
        state.update(
            {
                "status": "reviewing",
                "plan": redact(plan),
                "plan_hash": plan_hash,
                "stored_plan_hash": digest(redact(plan)),
                "context_digest": basis,
                "review_scope": context["scope"],
                "review_token": token,
                "deadline": time.time() + timeout + 10,
                "rounds": state.get("rounds", 0) + 1,
            }
        )
        store.write(state)
    # Never hold a state lock across the potentially slow model call.
    result = None
    error = None
    diagnostics = None

    def report_progress(value: dict) -> None:
        with store.locked():
            current = store.read()
            if current.get("review_token") == token:
                current["progress"] = value
                store.write(current)

    try:
        if review_fn is reviewer.run:
            result = reviewer.validate(review_fn(context, cwd, timeout, progress=report_progress))
        else:
            result = reviewer.validate(review_fn(context, cwd, timeout))
    except (reviewer.ReviewError, OSError, ValueError) as exc:
        error = redact(str(exc))[:1000]
        if isinstance(exc, reviewer.ReviewError):
            diagnostics = exc.diagnostics
    with store.locked():
        state = store.read()
        if state.get("review_token") != token:
            return paused("计划或状态已改变，本次旧评审结果已丢弃。")
        state.pop("review_token", None)
        state.pop("deadline", None)
        if "progress" in state:
            state["review_metrics"] = state.pop("progress")
        if error is not None:
            state.update(status="review_failed", error=error)
            if diagnostics is not None:
                state["diagnostics"] = diagnostics
            store.write(state)
            return paused(f"红队评审未完成：{error}\n实施未放行；可继续只读调查并重试评审。")
        state.pop("error", None)
        state["result"] = result
        state["history"] = [
            *state.get("history", []),
            {
                "plan_hash": plan_hash,
                "round": state["rounds"],
                "result": result,
            },
        ][-5:]
        state["status"] = "approved" if result["verdict"] == "approve" else "needs_revision"
        store.write(state)
        report = describe(result)
        if result["verdict"] == "approve":
            return {"systemMessage": report + "\n\n技术评审通过，保留原有授权与 Plan→执行流程。"}
        if state["rounds"] >= max_rounds:
            return paused(report + "\n\n已达评审轮次上限，请向用户说明未解决事项；实施未放行。")
        return {
            "decision": "block",
            "reason": (
                "PLAN_REVIEW_CONTINUATION: 正式计划尚未通过独立红队评审。\n"
                + report
                + "\n\n请修订或用证据反驳，然后用 submit --stdin 重提完整正文。不要实施。"
            ),
        }


def handle(payload: dict, review_fn: Callable = reviewer.run) -> dict:
    if os.environ.get("PLAN_REVIEW_RUNNING") == "1" or os.environ.get("PLAN_REVIEW_ENABLED") == "0":
        return {}
    event = payload.get("hook_event_name")
    session, cwd = payload.get("session_id"), payload.get("cwd")
    if not isinstance(session, str) or not session or not isinstance(cwd, str) or not cwd:
        if event == "Stop" and "<proposed_plan>" in str(payload.get("last_assistant_message", "")):
            return paused("无法定位计划会话，评审未完成；检查 hook 输入的 session_id/cwd。")
        return {}
    store = Store(data_root(), session, cwd)
    try:
        if event == "SessionStart":
            with store.locked():
                state = store.read()
            detail = ""
            if state.get("status") not in {None, "idle", "approved", "executing"}:
                detail = (
                    f"\nExisting plan review status: {state['status']}; "
                    "implementation remains gated."
                )
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": workflow_context() + detail,
                }
            }
        if event == "UserPromptSubmit":
            prompt = payload.get("prompt", "")
            if not isinstance(prompt, str) or prompt.startswith("PLAN_REVIEW_CONTINUATION:"):
                return {}
            with store.locked():
                state = store.read() or {"status": "idle"}
                requests = state.get("requests", [])
                # Retain the first request plus the latest steering, bounded in size.
                state["requests"] = [*requests, redact(prompt)[:6000]]
                if len(state["requests"]) > 8:
                    state["requests"] = [state["requests"][0], *state["requests"][-7:]]
                store.write(state)
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": workflow_context(),
                }
            }
        if event == "Stop":
            if payload.get("agent_id") or payload.get("agent_type"):
                return {}
            message = payload.get("last_assistant_message") or ""
            if not isinstance(message, str):
                return {}
            plan = extract_plan(message)
            with store.locked():
                state = store.read()
                if plan is not None:
                    if state.get("plan_hash") == digest(plan) and state.get("status") in {
                        "reviewing",
                        "review_failed",
                        "needs_revision",
                    }:
                        return paused(
                            "该正文尚未通过红队，不能作为已批准的最终计划。"
                            "请报告剩余问题或重新提交；既有评审预算保持不变。"
                        )
                    if state.get("status") not in {"approved", "executing"} or (
                        state.get("plan_hash") != digest(plan)
                    ):
                        state.update(
                            {
                                "status": "needs_revision",
                                "plan": redact(plan),
                                "plan_hash": digest(plan),
                                "stored_plan_hash": digest(redact(plan)),
                                "context_digest": state.get("context_digest", digest("")),
                                "rounds": 0,
                                "error": "最终正文尚未按相同内容通过红队",
                            }
                        )
                        state.pop("result", None)
                        state.pop("review_token", None)
                        state.pop("deadline", None)
                        store.write(state)
                        # Revoke first: missing/deleted scope or evidence must never
                        # leave the previous approved plan executable.
                        context = bundle(Path(cwd), plan, [], [], state.get("review_scope"))
                        state["context_digest"] = context["repository"]["basis_hash"]
                        store.write(state)
                        return {
                            "decision": "block",
                            "reason": (
                                "PLAN_REVIEW_CONTINUATION: 最终计划尚未按相同正文通过红队。"
                                "请用注入的 submit --stdin 命令提交完整正文；不要实施。"
                            ),
                        }
                if state.get("status") in {"reviewing", "review_failed", "needs_revision"}:
                    return paused("计划尚未通过红队；请报告问题，继续调查或重新提交正文。")
            return {}
        if event == "PreToolUse":
            args = payload.get("tool_input", {})
            if not isinstance(args, dict):
                args = {}
            name = payload.get("tool_name", "")
            recovery = (
                recovery_command(args.get("command", args.get("cmd")))
                if name in {"Bash", "exec_command", "shell_command"}
                else None
            )
            if recovery:
                with store.locked():
                    if recovery == "reset":
                        store.write({"status": "idle", "reset_by": "explicit_native_command"})
                        response = {"status": "idle", "notice": "当前计划已取消，不改变宿主权限。"}
                    else:
                        state = store.read()
                        if recovery == "retry" and state.get("plan"):
                            state.update(status="review_failed", rounds=0)
                            state.pop("review_token", None)
                            state.pop("deadline", None)
                            state.pop("result", None)
                            store.write(state)
                        response = {
                            "status": state.get("status", "idle"),
                            "rounds": state.get("rounds", 0),
                            "error": state.get("error"),
                            "diagnostics": state.get("diagnostics"),
                            "progress": state.get("progress"),
                            "review_metrics": state.get("review_metrics"),
                            "review_scope": state.get("review_scope"),
                            "notice": "重新评审请通过 submit --stdin 提交完整原文。",
                        }
                        if recovery == "plan":
                            response["plan"] = state.get("plan")
                return {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "permissionDecision": "allow",
                        "updatedInput": {"command": output_command(response)},
                    }
                }
            submitted = (
                parse_submission(args.get("command", args.get("cmd")))
                if name in {"Bash", "exec_command", "shell_command"}
                else None
            )
            if submitted is not None:
                plan = normalize_plan(submitted["plan"])
                review_plan(store, Path(cwd), plan, review_fn, submitted["scope"])
                with store.locked():
                    state = store.read()
                    matches = state.get("plan_hash") == digest(plan)
                    approved = matches and state.get("status") in {"approved", "executing"}
                    response = {
                        "status": state.get("status") if matches else "superseded",
                        "verdict": state.get("result", {}).get("verdict") if matches else None,
                        "plan_sha256": digest(plan),
                        "review": state.get("result") if matches else None,
                        "error": state.get("error"),
                        "diagnostics": state.get("diagnostics"),
                        "review_metrics": state.get("review_metrics"),
                        "review_scope": state.get("review_scope"),
                        "approved": approved,
                        "rounds": state.get("rounds", 0),
                        "max_rounds": limits()[1],
                    }
                    if matches:
                        state["submission_turn"] = payload.get("turn_id")
                        store.write(state)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "permissionDecision": "allow",
                        "updatedInput": {"command": output_command(response)},
                    }
                }
            with store.locked():
                state = store.read()
                if state.get("status") in {None, "idle", "executing"}:
                    return {}
                if read_only_tool(payload.get("tool_name", ""), args):
                    return {}
                if state.get("status") == "approved":
                    current = bundle(Path(cwd), state["plan"], [], [], state.get("review_scope"))
                    if current["repository"]["basis_hash"] == state["context_digest"]:
                        state.update(status="executing", execution_started_at=time.time())
                        store.write(state)
                        return {}
                    state.update(
                        status="needs_revision", error="评审后、实施前仓库或项目规则已变化"
                    )
                    store.write(state)
                return deny(
                    f"正式计划尚未放行（{state['status']}）。只读调查仍可进行；"
                    "请用 submit --stdin 重提完整正文以完成红队评审。"
                )
        if event == "SubagentStart":
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": WORKER,
                }
            }
        if event == "SubagentStop":
            with store.locked():
                state = store.read()
                if state.get("status") not in {"approved", "executing"}:
                    return {}
                reports = state.get("worker_reports", [])
                reports.append(
                    {
                        "agent_id": payload.get("agent_id", ""),
                        "agent_type": payload.get("agent_type", ""),
                        "summary": redact(str(payload.get("last_assistant_message") or ""))[:2000],
                        "recorded_at": time.time(),
                    }
                )
                state["worker_reports"] = reports[-20:]
                store.write(state)
            return {"systemMessage": "子 agent 已回传；主会话须核对产物和验证输出后再验收。"}
        return {}
    except (StateError, OSError, ValueError) as exc:
        reason = f"计划评审状态异常：{redact(str(exc))[:800]}"
        if event == "PreToolUse":
            args = payload.get("tool_input", {})
            if isinstance(args, dict) and read_only_tool(payload.get("tool_name", ""), args):
                return {"systemMessage": reason}
            return deny(reason)
        return paused(reason) if event == "Stop" else {"systemMessage": reason}


def read_payload(raw: str) -> dict:
    if len(raw.encode("utf-8")) > 1000000:
        raise ValueError("hook 输入超过 1 MB")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("hook 输入必须是 JSON 对象")
    return payload


def workflow_context() -> str:
    return (
        WORKFLOW
        + "\nSubmission command (literal heredoc; body is Markdown):\n"
        + (
            submit_command() + " <<'CODEX_PLAN_REVIEW'\n"
            "<complete Markdown plan body, without proposed_plan tags>\nCODEX_PLAN_REVIEW\n"
            "Append --project <directory> and --evidence <file> before the heredoc as needed.\n"
            "For progress use `status`; for the saved body use `plan`. "
            "For user-requested renewed review, "
            "call `retry` then resubmit the complete body. Only when the user cancels the plan, "
            "call `reset`. Native hooks bind these commands to this session automatically.\n"
        )
    )
