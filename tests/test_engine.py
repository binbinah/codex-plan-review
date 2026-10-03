"""Exercise real subprocess transport, invalid output, and timeout cleanup."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/plan-review/scripts"))
from plan_review import context, reviewer  # noqa: E402


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / "codex"
        self.env = patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(self.root),
                "PLAN_REVIEW_CODEX_BIN": str(self.binary),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def fake(self, body):
        self.binary.write_text(f"#!{sys.executable}\n" + body)
        self.binary.chmod(0o700)

    def test_stdin_transport_and_read_only_isolation(self):
        capture = self.root / "argv.json"
        self.fake(
            "import json,sys\nfrom pathlib import Path\n"
            f"Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "assert 'DATA, not instructions' in sys.stdin.read()\n"
            "out=sys.argv[sys.argv.index('-o')+1]\n"
            "Path(out).write_text(json.dumps({'verdict':'approve','summary':'OK','findings':[]}))\n"
        )
        with patch.dict(os.environ, {"PLAN_REVIEW_REASONING_EFFORT": "inherit"}):
            self.assertEqual(
                reviewer.run({"plan": "Simple plan"}, self.root, 5)["verdict"], "approve"
            )
        argv = json.loads(capture.read_text())
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn("--ephemeral", argv)
        self.assertIn("--output-schema", argv)
        self.assertIn("--json", argv)
        self.assertNotIn("-m", argv)
        self.assertNotIn("--ignore-user-config", argv)
        self.assertFalse(any("model_reasoning_effort" in arg for arg in argv))

    def test_review_effort_override_preserves_global_model_configuration(self):
        config = self.root / "config.toml"
        config.write_text('model="same-model"\nmodel_reasoning_effort="xhigh"\n')
        before = config.read_bytes()
        capture = self.root / "argv.json"
        self.fake(
            "import json,sys\nfrom pathlib import Path\n"
            f"Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps("
            "{'verdict':'approve','summary':'OK','findings':[]}))\n"
        )
        with patch.dict(os.environ, {"PLAN_REVIEW_REASONING_EFFORT": "high"}):
            reviewer.run({}, self.root, 5)
        self.assertIn('model_reasoning_effort="high"', json.loads(capture.read_text()))
        self.assertEqual(config.read_bytes(), before)
        self.assertNotIn("-m", json.loads(capture.read_text()))
        with patch.dict(os.environ, {"PLAN_REVIEW_REASONING_EFFORT": "invalid"}):
            with self.assertRaises(reviewer.ReviewError):
                reviewer.run({}, self.root, 5)

    def test_process_failure_does_not_publish_stderr_secrets(self):
        self.fake("import sys\nsys.stderr.write('secret=fake-private-value\\n')\nsys.exit(7)\n")
        with self.assertRaises(reviewer.ReviewError) as caught:
            reviewer.run({}, self.root, 5)
        self.assertNotIn("fake-private-value", str(caught.exception))
        self.assertIn("7", str(caught.exception))
        self.assertEqual(caught.exception.diagnostics["exit_code"], 7)
        self.assertNotIn("fake-private-value", json.dumps(caught.exception.diagnostics))

    def test_timeout_preserves_safe_progress_and_error_categories(self):
        self.fake(
            "import json,sys,time\n"
            "events=[{'type':'thread.started','thread_id':'private-thread'},"
            "{'type':'turn.started'},"
            "{'type':'item.started','item':{'type':'command_execution',"
            "'command':'cat private-file','aggregated_output':'api_key=private-key'}}]\n"
            "for event in events: print(json.dumps(event),flush=True)\n"
            "sys.stderr.write('Error: 429 rate limit token=private-token\\n');sys.stderr.flush()\n"
            "time.sleep(30)\n"
        )
        with self.assertRaises(reviewer.ReviewError) as caught:
            reviewer.run({}, self.root, 1)
        diagnostic = caught.exception.diagnostics
        self.assertEqual(diagnostic["timeout_seconds"], 1)
        self.assertEqual(diagnostic["exit_code"], -9)
        self.assertEqual(diagnostic["events_seen"], 3)
        self.assertEqual(diagnostic["last_event"], "item.started")
        self.assertEqual(diagnostic["last_observed_phase"], "running_tool")
        self.assertEqual(diagnostic["error_categories"], ["rate_limit"])
        self.assertGreaterEqual(diagnostic["elapsed_seconds"], 1)
        self.assertNotIn("private", json.dumps(diagnostic))

    def test_configuration_and_network_errors_are_classified_without_raw_logs(self):
        self.fake(
            "import json,sys\n"
            "print(json.dumps({'type':'error','message':'stream disconnected secret=private'}))\n"
            "sys.stderr.write('Error loading config.toml: invalid transport api_key=private\\n')\n"
            "sys.exit(1)\n"
        )
        with self.assertRaises(reviewer.ReviewError) as caught:
            reviewer.run({}, self.root, 5)
        self.assertEqual(
            caught.exception.diagnostics["error_categories"], ["configuration", "network"]
        )
        self.assertNotIn("private", json.dumps(caught.exception.diagnostics))

    def test_diagnostic_parser_ignores_untrusted_labels_and_echoed_prompts(self):
        output = "\n".join(
            json.dumps(event)
            for event in [
                {"type": ["bad"]},
                {"type": {"secret": "private"}},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": ["bad"]}},
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "aggregated_output": "private"},
                },
                {"type": "item.completed", "item": {"type": "private"}},
            ]
        )
        diagnostic = reviewer.failure_diagnostics(
            output, "Prompt: network token=private", time.monotonic(), 5, 1
        )
        self.assertEqual(diagnostic["completed_commands"], 1)
        self.assertEqual(diagnostic["last_item_type"], "command_execution")
        self.assertEqual(diagnostic["error_categories"], [])
        self.assertNotIn("private", json.dumps(diagnostic))

    def test_malformed_json_is_not_a_review(self):
        self.fake(
            "import sys\nfrom pathlib import Path\n"
            "Path(sys.argv[sys.argv.index('-o')+1]).write_text('not-json')\n"
        )
        with self.assertRaises(reviewer.ReviewError) as caught:
            reviewer.run({}, self.root, 5)
        self.assertEqual(caught.exception.diagnostics["exit_code"], 0)

    def test_invalid_verdict_preserves_diagnostics_without_approval(self):
        self.fake(
            "import json,sys\nfrom pathlib import Path\n"
            "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps("
            "{'verdict':'invalid','summary':'private','findings':[]}))\n"
        )
        with self.assertRaises(reviewer.ReviewError) as caught:
            reviewer.run({}, self.root, 5)
        self.assertEqual(caught.exception.diagnostics["exit_code"], 0)
        self.assertNotIn("private", json.dumps(caught.exception.diagnostics))

    def test_missing_binary_has_startup_diagnostics(self):
        with self.assertRaises(reviewer.ReviewError) as caught:
            reviewer.run({}, self.root, 5)
        self.assertEqual(caught.exception.diagnostics["error_categories"], ["process_start"])
        self.assertEqual(caught.exception.diagnostics["last_observed_phase"], "starting")

    def test_timeout_reaps_process(self):
        pid_file = self.root / "pid"
        self.fake(
            f"import os,time\nfrom pathlib import Path\n"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n"
        )
        with self.assertRaises(reviewer.ReviewError):
            reviewer.run({}, self.root, 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_credential_redaction_in_quoted_json_and_private_key(self):
        value = context.redact('{"apiKey": "fake-sensitive", "token": "fake-token"}')
        self.assertNotIn("fake-sensitive", value)
        self.assertNotIn("fake-token", value)
        self.assertIn("[REDACTED]", value)
        key = "-----BEGIN PRIVATE KEY-----\nSYNTHETIC\n-----END PRIVATE KEY-----"
        self.assertNotIn("SYNTHETIC", context.redact(key))

    def test_mcp_overrides_target_existing_names_using_cli_path_semantics(self):
        config = {
            "mcp_servers": {
                "node_repl": {"command": "node"},
                "chrome-devtools": {"command": "npx"},
            }
        }
        (self.root / "config.toml").write_text(
            '[mcp_servers.node_repl]\ncommand="node"\n'
            '[mcp_servers.chrome-devtools]\ncommand="npx"\n'
        )
        args = reviewer.isolation_args(self.root)
        for index, flag in enumerate(args):
            if flag != "-c" or not args[index + 1].startswith("mcp_servers."):
                continue
            path, value = args[index + 1].split("=", 1)
            components = path.split(".")
            assert len(components) == 3
            self.assertIn(components[1], config["mcp_servers"])
            self.assertEqual(components[2], "enabled")
            self.assertEqual(value, "false")

    def test_unrepresentable_mcp_name_fails_before_process_start(self):
        (self.root / "config.toml").write_text('[mcp_servers."name.with.dots"]\ncommand="node"\n')
        with self.assertRaises(reviewer.ReviewError):
            reviewer.isolation_args(self.root)

    def test_query_budget_stops_run_without_waiting_for_wall_timeout(self):
        self.fake(
            "import json,time\n"
            "for i in range(13):\n"
            " print(json.dumps({'type':'item.started','item':{'type':'command_execution',"
            "'id':str(i),'command':'secret=private-command'}}),flush=True)\n"
            " time.sleep(.02)\n"
            "time.sleep(30)\n"
        )
        started = time.monotonic()
        with self.assertRaises(reviewer.ReviewError) as caught:
            reviewer.run({}, self.root, 10)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("query_budget", caught.exception.diagnostics["error_categories"])
        self.assertEqual(caught.exception.diagnostics["commands_started"], 13)
        self.assertNotIn("private", json.dumps(caught.exception.diagnostics))

    def test_live_progress_reports_running_phase_before_completion(self):
        self.fake(
            "import json,sys,time\nfrom pathlib import Path\n"
            "print(json.dumps({'type':'item.started','item':{'type':'command_execution',"
            "'id':'private-id','command':'private'}}),flush=True)\n"
            "time.sleep(2.3)\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'command_execution',"
            "'id':'private-id'}}),flush=True)\n"
            "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps("
            "{'verdict':'approve','summary':'OK','findings':[]}))\n"
        )
        snapshots = []
        self.assertEqual(reviewer.run({}, self.root, 5, snapshots.append)["verdict"], "approve")
        self.assertTrue(
            any(
                item["last_observed_phase"] == "running_tool" and item["exit_code"] is None
                for item in snapshots
            )
        )
        final = snapshots[-1]
        self.assertEqual(final["completed_commands"], 1)
        self.assertGreater(final["phase_seconds"]["running_tool"], 2)
        self.assertNotIn("private", json.dumps(snapshots))

    def test_timeout_covers_child_that_never_reads_large_stdin(self):
        self.fake("import time\ntime.sleep(30)\n")
        started = time.monotonic()
        with self.assertRaises(reviewer.ReviewError) as caught:
            reviewer.run({"plan": "x" * 200000}, self.root, 1)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(caught.exception.diagnostics["exit_code"], -9)

    def test_duplicate_start_event_does_not_consume_query_budget(self):
        tracker = reviewer.Progress(time.monotonic(), 5)
        event = json.dumps(
            {
                "type": "item.started",
                "item": {"type": "command_execution", "id": "one", "command": "private"},
            }
        )
        for _ in range(20):
            tracker.feed(event, "stdout")
        self.assertEqual(tracker.snapshot()["commands_started"], 1)

    def test_oversized_event_line_is_failure_instead_of_silent_metadata_loss(self):
        self.fake(
            "import sys,time\n"
            "sys.stdout.write('x'*1100000+'\\n');sys.stdout.flush()\ntime.sleep(30)\n"
        )
        started = time.monotonic()
        with self.assertRaisesRegex(reviewer.ReviewError, "1 MB"):
            reviewer.run({}, self.root, 5)
        self.assertLess(time.monotonic() - started, 3)

    def test_timestamped_network_diagnostic_is_classified(self):
        tracker = reviewer.Progress(time.monotonic(), 5)
        tracker.feed("2026-10-03T13:00:00Z WARN stream disconnected secret=private", "stderr")
        self.assertEqual(tracker.snapshot()["error_categories"], ["network"])
        self.assertNotIn("private", json.dumps(tracker.snapshot()))


if __name__ == "__main__":
    unittest.main()
