"""Behavioral tests for the hook state machine and Codex protocol adapters."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/plan-review/scripts"
sys.path.insert(0, str(SCRIPTS))

from plan_review import context, hooks, policy, reviewer, state, submission  # noqa: E402
from plan_review.state import StateError, Store, digest  # noqa: E402

APPROVE = {"verdict": "approve", "summary": "No blocking issues.", "findings": []}
CONCERNS = {
    "verdict": "concerns",
    "summary": "Missing verification.",
    "findings": [
        {
            "severity": "major",
            "verified": True,
            "evidence": "Plan omits validation.",
            "impact": "Behavior may regress.",
            "fix": "Add a meaningful acceptance command.",
        }
    ],
}
REJECT = {
    "verdict": "reject",
    "summary": "Wrong result.",
    "findings": [
        {
            "severity": "critical",
            "verified": True,
            "evidence": "Plan returns 0 for 2+3.",
            "impact": "Addition becomes incorrect.",
            "fix": "Preserve addition semantics.",
        }
    ],
}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.cwd = self.path / "repo"
        self.cwd.mkdir()
        self.home = self.path / "home"
        self.home.mkdir()
        self.env = patch.dict(
            os.environ,
            {
                "PLAN_REVIEW_DATA_DIR": str(self.path / "state"),
                "CODEX_HOME": str(self.home),
                "PLAN_REVIEW_MAX_ROUNDS": "3",
                "PLAN_REVIEW_TIMEOUT_SECONDS": "2",
                "PLAN_REVIEW_RUNNING": "0",
                "PLAN_REVIEW_ENABLED": "1",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store(self.path / "state", "session", str(self.cwd))

    def event(self, name, review=None, **fields):
        return hooks.handle(
            {"hook_event_name": name, "session_id": "session", "cwd": str(self.cwd), **fields},
            review_fn=review or (lambda *_: APPROVE),
        )

    def submit(self, text="Implement the exact requested behavior.", review=None):
        return hooks.review_plan(self.store, self.cwd, text, review or (lambda *_: APPROVE))

    def direct_submit(self, text="Implement the exact requested behavior.", review=None):
        result = self.event(
            "PreToolUse",
            review,
            tool_name="Bash",
            turn_id="turn",
            tool_input={
                "command": submission.submit_command() + " <<'PLAN_BODY'\n" + text + "\nPLAN_BODY"
            },
        )
        command = result["hookSpecificOutput"]["updatedInput"]["command"]
        output = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        self.assertEqual(output.returncode, 0)
        return json.loads(output.stdout)

    def state(self):
        with self.store.locked():
            return self.store.read()

    def tool(self, name, args):
        return self.event("PreToolUse", tool_name=name, tool_input=args)

    def test_ordinary_session_does_not_gate_edits(self):
        self.assertEqual(self.event("Stop", last_assistant_message="A normal answer."), {})
        self.assertEqual(self.tool("apply_patch", {"command": "patch"}), {})

    def test_quoted_plan_example_is_not_a_submission(self):
        self.assertEqual(
            self.event("Stop", last_assistant_message="Example:\n<proposed_plan>x</proposed_plan>"),
            {},
        )

    def test_session_start_explains_plan_protocol_without_lock(self):
        self.assertIn(
            "proposed_plan", self.event("SessionStart")["hookSpecificOutput"]["additionalContext"]
        )
        self.assertEqual(self.tool("Bash", {"command": "touch file"}), {})

    def test_completed_plan_is_reviewed_once_then_native_transition(self):
        calls = []

        def engine(*args):
            calls.append(args)
            return APPROVE

        result = self.submit(review=engine)
        self.assertNotIn("decision", result)
        self.assertNotIn("continue", result)
        self.assertEqual(self.state()["status"], "approved")
        self.submit(review=engine)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.tool("apply_patch", {"command": "patch"}), {})
        self.assertEqual(self.state()["status"], "executing")

    def test_native_implement_prompt_does_not_demand_a_hash_phrase(self):
        self.submit()
        self.event("UserPromptSubmit", prompt="Implement the plan.")
        self.assertEqual(self.tool("spawn_agent", {"agent_type": "worker"}), {})

    def test_changed_plan_is_reviewed_again_with_new_cycle(self):
        self.submit()
        self.submit("A materially different plan.", lambda *_: REJECT)
        self.assertEqual(self.state()["status"], "needs_revision")
        self.assertEqual(self.state()["rounds"], 1)

    def test_reject_blocks_codex_patch_and_worker_but_allows_reads(self):
        self.submit(review=lambda *_: REJECT)
        for name, args in [
            ("apply_patch", {"command": "patch"}),
            ("Bash", {"command": "python3 writer.py"}),
            ("spawn_agent", {"agent_type": "worker"}),
        ]:
            with self.subTest(name=name):
                self.assertEqual(
                    self.tool(name, args)["hookSpecificOutput"]["permissionDecision"], "deny"
                )
        for name, args in [
            ("Bash", {"command": "rg -n pattern file | head -n 5"}),
            ("Bash", {"command": "git diff --stat"}),
            ("Read", {}),
        ]:
            self.assertEqual(self.tool(name, args), {})

    def test_concerns_continue_revision_without_authorizing(self):
        result = self.submit(review=lambda *_: CONCERNS)
        self.assertEqual(result["decision"], "block")
        self.assertEqual(self.state()["status"], "needs_revision")

    def test_rebuttal_carries_findings_to_independent_reviewer(self):
        self.submit(review=lambda *_: CONCERNS)
        captured = []
        self.submit("Validation command added.", lambda *args: captured.append(args[0]) or APPROVE)
        self.assertEqual(captured[0]["previous_reviews"][0]["result"]["verdict"], "concerns")

    def test_engine_failure_never_approves(self):
        def fail(*_):
            raise reviewer.ReviewError("Engine unavailable")

        result = self.submit(review=fail)
        self.assertFalse(result["continue"])
        self.assertEqual(self.state()["status"], "review_failed")
        self.assertEqual(
            self.tool("apply_patch", {})["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_failure_diagnostics_survive_submission_and_clear_on_new_review(self):
        diagnostic = {"timeout_seconds": 600, "last_observed_phase": "waiting_for_model"}

        def fail(*_):
            raise reviewer.ReviewError("Timed out", diagnostic)

        response = self.direct_submit(review=fail)
        self.assertFalse(response["approved"])
        self.assertIsNone(response["verdict"])
        self.assertEqual(response["diagnostics"], diagnostic)
        self.assertEqual(self.state()["diagnostics"], diagnostic)
        self.assertTrue(self.direct_submit()["approved"])
        self.assertNotIn("diagnostics", self.state())

    def test_invalid_verdict_never_approves(self):
        result = self.submit(review=lambda *_: {**REJECT, "verdict": "approve"})
        self.assertFalse(result["continue"])
        self.assertEqual(self.state()["status"], "review_failed")

    def test_round_budget_stops_without_bypassing(self):
        for _ in range(3):
            result = self.submit(review=lambda *_: CONCERNS)
        self.assertFalse(result["continue"])
        result = self.submit(review=lambda *_: APPROVE)
        self.assertFalse(result["continue"])
        self.assertEqual(self.state()["rounds"], 3)

    def test_resume_retains_review_lock(self):
        self.submit(review=lambda *_: REJECT)
        self.assertIn(
            "needs_revision", self.event("SessionStart")["hookSpecificOutput"]["additionalContext"]
        )
        self.assertEqual(
            self.tool("apply_patch", {})["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_instruction_change_before_execution_invalidates_review(self):
        self.submit()
        (self.cwd / "AGENTS.md").write_text("New project constraints.")
        self.assertEqual(
            self.tool("apply_patch", {})["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertEqual(self.state()["status"], "needs_revision")

    def test_untracked_content_change_before_execution_invalidates_review(self):
        subprocess.run(["git", "init", str(self.cwd)], capture_output=True, check=True)
        file = self.cwd / "new-source.py"
        file.write_text("value = 1\n")
        self.submit()
        file.write_text("value = 2\n")
        self.assertEqual(
            self.tool("apply_patch", {})["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_corrupt_state_blocks_mutation_but_preserves_reads(self):
        self.submit()
        self.store.path.write_text("broken")
        self.assertEqual(
            self.tool("apply_patch", {})["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertNotIn("hookSpecificOutput", self.tool("Bash", {"command": "cat file"}))

    def test_plan_content_change_without_hash_is_rejected(self):
        self.submit()
        state = json.loads(self.store.path.read_text())
        state["plan"] = "Different plan"
        self.store.path.write_text(json.dumps(state))
        with self.assertRaises(StateError):
            self.state()

    def test_state_scope_includes_project_and_session(self):
        self.submit(review=lambda *_: REJECT)
        result = hooks.handle(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "other",
                "cwd": str(self.cwd),
                "tool_name": "apply_patch",
                "tool_input": {},
            }
        )
        self.assertEqual(result, {})

    def test_state_files_are_private(self):
        self.submit()
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)

    def test_parallel_same_plan_launches_one_engine(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def engine(*args):
            calls.append(args)
            entered.set()
            self.assertTrue(release.wait(4))
            return APPROVE

        with concurrent.futures.ThreadPoolExecutor() as pool:
            first = pool.submit(self.submit, review=engine)
            self.assertTrue(entered.wait(2))
            second = self.submit(review=engine)
            release.set()
            first.result()
        self.assertFalse(second["continue"])
        self.assertEqual(len(calls), 1)

    def test_superseded_review_cannot_publish_old_approval(self):
        def reset_during_review(*_):
            with self.store.locked():
                self.store.write({"status": "idle"})
            return APPROVE

        result = self.submit(review=reset_during_review)
        self.assertFalse(result["continue"])
        self.assertEqual(self.state()["status"], "idle")

    def test_recursive_reviewer_is_excluded(self):
        with patch.dict(os.environ, {"PLAN_REVIEW_RUNNING": "1"}):
            self.assertEqual(
                self.event(
                    "PreToolUse",
                    tool_name="Bash",
                    tool_input={"command": submission.submit_command() + " <<'PLAN'\nPlan\nPLAN"},
                ),
                {},
            )

    def test_continuation_prompt_is_not_a_new_user_requirement(self):
        self.event("UserPromptSubmit", prompt="Original requirement")
        self.event("UserPromptSubmit", prompt="PLAN_REVIEW_CONTINUATION: fix issue")
        self.assertEqual(self.state()["requests"], ["Original requirement"])

    def test_worker_returns_are_recorded_without_certifying_success(self):
        self.submit()
        self.event(
            "SubagentStop", agent_id="worker-1", agent_type="worker", last_assistant_message="Done"
        )
        self.assertEqual(self.state()["worker_reports"][0]["agent_id"], "worker-1")
        self.assertEqual(self.state()["status"], "approved")

    def test_recovery_reset_is_explicit_and_local(self):
        self.submit(review=lambda *_: REJECT)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "review.py"),
                "reset",
                "--session",
                "session",
                "--cwd",
                str(self.cwd),
            ],
            capture_output=True,
            text=True,
            env=os.environ,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.state()["status"], "idle")

    def test_empty_formal_plan_pauses(self):
        result = self.event("Stop", last_assistant_message="<proposed_plan></proposed_plan>")
        self.assertFalse(result["continue"])

    def test_direct_body_submission_is_bound_to_hook_session(self):
        response = self.direct_submit()
        self.assertTrue(response["approved"])
        self.assertEqual(response["verdict"], "approve")
        self.assertEqual(self.state()["submission_turn"], "turn")
        self.assertEqual(response["plan_sha256"], self.state()["plan_hash"])

    def test_stop_with_null_message_never_reads_transcript_or_calls_engine(self):
        self.direct_submit()

        def unexpected(*_):
            raise AssertionError("Stop must not run a review")

        self.assertEqual(
            self.event(
                "Stop",
                unexpected,
                last_assistant_message=None,
                transcript_path="/path/that/must/not/be/read",
            ),
            {},
        )

    def test_final_body_change_invalidates_prior_approval(self):
        self.direct_submit()
        result = self.event(
            "Stop", last_assistant_message="<proposed_plan>Different plan</proposed_plan>"
        )
        self.assertEqual(result["decision"], "block")
        self.assertEqual(self.state()["status"], "needs_revision")
        self.assertEqual(
            self.tool("apply_patch", {})["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_direct_revision_is_usable_while_locked(self):
        first = self.direct_submit(review=lambda *_: REJECT)
        self.assertFalse(first["approved"])
        second = self.direct_submit("Corrected plan with verification.")
        self.assertTrue(second["approved"])

    def test_finalizing_rejected_body_does_not_reset_review_budget(self):
        plan = "Plan with an unresolved blocker."
        for _ in range(3):
            self.direct_submit(plan, lambda *_: REJECT)
        self.event("Stop", last_assistant_message=f"<proposed_plan>{plan}</proposed_plan>")
        response = self.direct_submit(plan)
        self.assertFalse(response["approved"])
        self.assertEqual(self.state()["rounds"], 3)

    def test_submission_shell_text_is_data_not_executable(self):
        plan = "Review these literals: $(touch NEVER_CREATE), `touch OTHER_FILE`, and 'quotes'."
        result = self.direct_submit(plan)
        self.assertTrue(result["approved"])
        self.assertFalse((self.cwd / "NEVER_CREATE").exists())
        self.assertFalse((self.cwd / "OTHER_FILE").exists())

    def test_direct_engine_failure_has_no_stale_approve(self):
        self.direct_submit()

        def fail(*_):
            raise reviewer.ReviewError("Failed")

        result = self.direct_submit("A new plan", fail)
        self.assertFalse(result["approved"])
        self.assertIsNone(result["verdict"])

    def test_direct_submit_without_trusted_hook_fails_clearly(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "review.py"), "submit", "--stdin"],
            input="Plan",
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("PreToolUse", result.stderr)

    def test_native_recovery_is_available_while_locked_without_session_guess(self):
        self.direct_submit(review=lambda *_: REJECT)
        base = submission.submit_command().removesuffix(" submit --stdin")
        result = self.tool("Bash", {"command": base + " retry"})
        self.assertIn("updatedInput", result["hookSpecificOutput"])
        self.assertEqual(self.state()["rounds"], 0)
        self.assertTrue(self.direct_submit("Corrected plan")["approved"])
        self.tool("Bash", {"command": base + " reset"})
        self.assertEqual(self.state()["status"], "idle")

    def test_native_reset_can_recover_corrupt_state(self):
        self.direct_submit()
        self.store.path.write_text("broken")
        base = submission.submit_command().removesuffix(" submit --stdin")
        self.tool("Bash", {"command": base + " reset"})
        self.assertEqual(self.state()["status"], "idle")

    def test_saved_plan_and_progress_are_readable_while_failed(self):
        self.direct_submit("Complete saved body", review=lambda *_: REJECT)
        base = submission.submit_command().removesuffix(" submit --stdin")
        for action in ("plan", "status"):
            result = self.tool("Bash", {"command": base + " " + action})
            output = subprocess.check_output(
                ["sh", "-c", result["hookSpecificOutput"]["updatedInput"]["command"]], text=True
            )
            value = json.loads(output)
            self.assertEqual(value["status"], "needs_revision")
            if action == "plan":
                self.assertEqual(value["plan"], "Complete saved body")

    def test_native_scope_is_saved_and_second_project_edit_blocks_execution(self):
        second = self.path / "second project"
        second.mkdir()
        for root in (self.cwd, second):
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        source = second / "logic.py"
        source.write_text("answer = 1\n")
        seen = []

        def engine(value, *_):
            seen.append(value)
            return APPROVE

        command = (
            submission.submit_command()
            + " --project "
            + shlex.quote(str(self.cwd))
            + " --project "
            + shlex.quote(str(second))
            + " --evidence "
            + shlex.quote(str(source))
            + " <<'BODY'\nReview both\nBODY"
        )
        self.event("PreToolUse", engine, tool_name="Bash", tool_input={"command": command})
        self.assertEqual(
            self.state()["review_scope"]["projects"],
            [str(self.cwd.resolve()), str(second.resolve())],
        )
        self.assertEqual(len(seen[0]["repository"]["projects"]), 2)
        source.write_text("answer = 2\n")
        result = self.tool("apply_patch", {"command": "patch"})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.state()["status"], "needs_revision")

    def test_invalid_new_scope_revokes_previous_approval(self):
        self.direct_submit("Approved old body")
        command = (
            submission.submit_command()
            + " --project /missing/project"
            + " <<'BODY'\nNew unreviewed body\nBODY"
        )
        result = self.tool("Bash", {"command": command})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.state()["status"], "needs_revision")
        self.assertNotIn("result", self.state())
        self.assertEqual(
            self.tool("apply_patch", {"command": "patch"})["hookSpecificOutput"][
                "permissionDecision"
            ],
            "deny",
        )

    def test_changed_final_body_revokes_approval_even_when_evidence_disappears(self):
        subprocess.run(["git", "-C", str(self.cwd), "init", "-q"], check=True)
        source = self.cwd / "logic.py"
        source.write_text("answer = 1\n")
        command = (
            submission.submit_command()
            + " --project "
            + shlex.quote(str(self.cwd))
            + " --evidence "
            + shlex.quote(str(source))
            + " <<'BODY'\nApproved body\nBODY"
        )
        self.tool("Bash", {"command": command})
        self.assertEqual(self.state()["status"], "approved")
        source.unlink()
        result = self.event(
            "Stop", last_assistant_message="<proposed_plan>Changed body</proposed_plan>"
        )
        self.assertFalse(result["continue"])
        self.assertEqual(self.state()["status"], "needs_revision")
        self.assertNotIn("result", self.state())

    def test_malformed_persisted_scope_denies_tools_without_hook_failure(self):
        self.direct_submit()
        with self.store.locked():
            value = self.store.read()
            value["review_scope"] = {"unexpected": "value"}
            self.store.write(value)
        result = self.tool("apply_patch", {"command": "patch"})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_real_engine_progress_is_durable_before_run_finishes(self):
        binary = self.path / "fake-codex"
        binary.write_text(
            f"#!{sys.executable}\nimport json,sys,time\nfrom pathlib import Path\n"
            "print(json.dumps({'type':'item.started','item':{'type':'command_execution',"
            "'id':'one','command':'private'}}),flush=True)\n"
            "time.sleep(2.5)\n"
            "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps("
            "{'verdict':'approve','summary':'OK','findings':[]}))\n"
        )
        binary.chmod(0o700)
        with (
            patch.dict(
                os.environ,
                {"PLAN_REVIEW_CODEX_BIN": str(binary), "PLAN_REVIEW_TIMEOUT_SECONDS": "5"},
            ),
            concurrent.futures.ThreadPoolExecutor() as pool,
        ):
            task = pool.submit(hooks.review_plan, self.store, self.cwd, "Review", reviewer.run)
            deadline = time.monotonic() + 4
            running = None
            while time.monotonic() < deadline:
                value = self.state()
                if value.get("progress", {}).get("last_observed_phase") == "running_tool":
                    running = value
                    break
                time.sleep(0.05)
            task.result(timeout=5)
        self.assertIsNotNone(running)
        self.assertEqual(running["status"], "reviewing")
        self.assertNotIn("private", json.dumps(running["progress"]))
        self.assertEqual(self.state()["review_metrics"]["commands_started"], 1)


class ProtocolTests(unittest.TestCase):
    def test_installed_standalone_commands_find_the_hooks_state_directory(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ):
            for name in ("PLAN_REVIEW_DATA_DIR", "PLUGIN_DATA"):
                os.environ.pop(name, None)
            os.environ["CODEX_HOME"] = td
            module = (
                Path(td)
                / "plugins/cache/codex-plan-review/plan-review/0.1.3/scripts/plan_review/state.py"
            )
            with patch.object(state, "__file__", str(module)):
                expected = Path(td) / "plugins/data/plan-review-codex-plan-review/plan-review"
                self.assertEqual(state.data_root().resolve(), expected.resolve())

    def test_scoped_submission_parser_rejects_unknown_or_incomplete_options(self):
        command = (
            submission.submit_command() + " --project '/tmp/repo with spaces' --evidence /tmp/file"
        )
        good = command + " <<'BODY'\nPlan\nBODY"
        value = submission.parse_submission(good)
        self.assertEqual(value["scope"]["projects"], ["/tmp/repo with spaces"])
        for header in [command + " --project", command + " --unknown /tmp", command + " ; touch x"]:
            self.assertIsNone(submission.parse_submission(header + " <<'BODY'\nPlan\nBODY"))

    def test_common_read_commands_work_but_git_config_and_sed_execution_do_not(self):
        for command in [
            "git -C '/tmp/repo with spaces' status --short",
            "git --no-pager -C/tmp/repo diff --stat",
            "sed -n '1,30p' file.json",
            "sed -n -e '1,$p' file.json",
        ]:
            self.assertTrue(policy.read_only_shell(command), command)
        for command in [
            "git -C /tmp/repo -c alias.status=writer status",
            "git -C /tmp/repo checkout main",
            "git -C /tmp/repo diff --ext-diff",
            "sed -i '1d' file",
            "sed '1e' file",
            "sed '1w output' file",
            "sed -f script file",
            "sed -n '1p; e writer' file",
        ]:
            self.assertFalse(policy.read_only_shell(command), command)

    def test_oversized_review_does_not_get_approved(self):
        with self.assertRaises(reviewer.ReviewError):
            reviewer.validate({**APPROVE, "summary": "x" * 601})
        with self.assertRaises(reviewer.ReviewError):
            reviewer.validate({**CONCERNS, "findings": CONCERNS["findings"] * 6})

    def test_review_budget_fits_host_hook_budget(self):
        with patch.dict(os.environ):
            os.environ.pop("PLAN_REVIEW_TIMEOUT_SECONDS", None)
            self.assertEqual(hooks.limits()[0], 600)
        with patch.dict(os.environ, {"PLAN_REVIEW_TIMEOUT_SECONDS": "900"}):
            self.assertEqual(hooks.limits()[0], 900)
        for timeout in ["0", "901", "invalid"]:
            with patch.dict(os.environ, {"PLAN_REVIEW_TIMEOUT_SECONDS": timeout}):
                with self.assertRaises(StateError):
                    hooks.limits()
        config = json.loads((ROOT / "plugins/plan-review/hooks/hooks.json").read_text())
        self.assertGreaterEqual(
            config["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"], reviewer.MAX_TIMEOUT + 60
        )

    def test_submission_parser_rejects_extra_shell_and_other_script(self):
        good = submission.submit_command() + " <<'PLAN'\nComplete plan\nPLAN"
        self.assertEqual(submission.extract_submission(good), "Complete plan")
        for bad in [
            good + "\ntouch another",
            good.replace("<<'PLAN'", "<<PLAN"),
            good.replace("review.py", "other.py"),
            good.replace("Complete plan", "PLAN\ntouch another"),
        ]:
            self.assertIsNone(submission.extract_submission(bad), bad)

    def test_verdict_severity_consistency(self):
        for result in [APPROVE, CONCERNS, REJECT]:
            self.assertEqual(reviewer.validate(result), result)
        with self.assertRaises(reviewer.ReviewError):
            reviewer.validate({**REJECT, "verdict": "approve"})

    def test_unverified_critical_cannot_be_reject(self):
        unverified = {**REJECT, "findings": [{**REJECT["findings"][0], "verified": False}]}
        with self.assertRaises(reviewer.ReviewError):
            reviewer.validate(unverified)
        self.assertEqual(
            reviewer.validate({**unverified, "verdict": "concerns"})["verdict"], "concerns"
        )

    def test_missing_evidence_is_not_accepted(self):
        with self.assertRaises(reviewer.ReviewError):
            reviewer.validate(
                {**CONCERNS, "findings": [{**CONCERNS["findings"][0], "evidence": ""}]}
            )

    def test_redaction_preserves_structured_result(self):
        result = reviewer.validate({**APPROVE, "summary": "api_key=sk-abcdefghijklmnop"})
        self.assertIn("[REDACTED]", result["summary"])
        self.assertNotIn("abcdefghijklmnop", result["summary"])

    def test_shell_classifier_rejects_interpreter_and_redirect_writes(self):
        for command in [
            "python3 -c 'write()'",
            "cat file > out",
            "cat file >| out",
            "cat file &>> out",
            "find . -delete",
            "rg --pre writer.py pattern",
            "git diff --output=out",
            "ls; touch out",
        ]:
            self.assertFalse(policy.read_only_shell(command), command)
        for command in ["git diff --stat", "rg pattern file | head -n 2", "cat file && wc -l file"]:
            self.assertTrue(policy.read_only_shell(command), command)

    def test_mcp_tools_are_not_assumed_read_only(self):
        self.assertFalse(policy.read_only_tool("mcp__service__get_then_update", {}))

    def test_context_prefers_agents_and_uses_only_configured_fallback(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CODEX_HOME": td}):
            root = Path(td) / "repo"
            root.mkdir()
            (root / "AGENTS.md").write_text("Actual rules")
            (root / "CLAUDE.md").write_text("Wrong rules")
            standards = context.instructions(root)
            self.assertEqual(standards[0]["content"], "Actual rules")

    def test_isolation_preserves_provider_but_disables_mcp_and_hooks(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CODEX_HOME": td}):
            (Path(td) / "config.toml").write_text('[mcp_servers.danger]\ncommand="writer"\n')
            args = reviewer.isolation_args(Path(td))
            self.assertIn("mcp_servers.danger.enabled=false", args)
            self.assertIn("hooks", args)
            self.assertNotIn("--ignore-user-config", args)

    def test_hash_is_sha256(self):
        self.assertEqual(len(digest("plan")), 64)


if __name__ == "__main__":
    unittest.main()
