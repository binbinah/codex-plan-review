"""Exercise real subprocess transport, invalid output, and timeout cleanup."""

from __future__ import annotations

import json
import os
import sys
import tempfile
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
        self.assertEqual(reviewer.run({"plan": "Simple plan"}, self.root, 5)["verdict"], "approve")
        argv = json.loads(capture.read_text())
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn("--ephemeral", argv)
        self.assertIn("--output-schema", argv)
        self.assertNotIn("-m", argv)
        self.assertNotIn("--ignore-user-config", argv)

    def test_process_failure_does_not_publish_stderr_secrets(self):
        self.fake("import sys\nsys.stderr.write('secret=fake-private-value\\n')\nsys.exit(7)\n")
        with self.assertRaises(reviewer.ReviewError) as caught:
            reviewer.run({}, self.root, 5)
        self.assertNotIn("fake-private-value", str(caught.exception))
        self.assertIn("7", str(caught.exception))

    def test_malformed_json_is_not_a_review(self):
        self.fake(
            "import sys\nfrom pathlib import Path\n"
            "Path(sys.argv[sys.argv.index('-o')+1]).write_text('not-json')\n"
        )
        with self.assertRaises(reviewer.ReviewError):
            reviewer.run({}, self.root, 5)

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


if __name__ == "__main__":
    unittest.main()
