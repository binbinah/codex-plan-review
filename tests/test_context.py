"""Verify multi-project evidence binding against real isolated Git repositories."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/plan-review/scripts"))
from plan_review import context  # noqa: E402


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "home")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def repo(self, name):
        root = self.workspace / name
        root.mkdir()
        (root / "logic.py").write_text("answer = 1\n")
        for arguments in [
            ["init", "-q"],
            ["add", "logic.py"],
            [
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
        ]:
            subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)
        return root

    def test_workspace_requires_explicit_projects(self):
        self.repo("first")
        with self.assertRaisesRegex(ValueError, "--project"):
            context.bundle(self.workspace, "Review first", [], [])

    def test_either_project_change_invalidates_basis_without_scanning_other_repos(self):
        first, second, unrelated = (self.repo(name) for name in ("first", "second", "other"))
        scope = {"projects": [str(first), str(second)], "evidence_files": []}

        def snapshot():
            return context.bundle(self.workspace, "Review both", [], [], scope)

        initial = snapshot()
        (unrelated / "logic.py").write_text("answer = 99\n")
        self.assertEqual(
            initial["repository"]["basis_hash"], snapshot()["repository"]["basis_hash"]
        )
        (first / "logic.py").write_text("answer = 2\n")
        changed = snapshot()
        self.assertNotEqual(
            initial["repository"]["basis_hash"], changed["repository"]["basis_hash"]
        )
        (second / "extra.py").write_text("value = 3\n")
        self.assertNotEqual(
            changed["repository"]["basis_hash"], snapshot()["repository"]["basis_hash"]
        )
        self.assertEqual(len(initial["repository"]["projects"]), 2)
        self.assertTrue(all(item["head"] for item in initial["repository"]["projects"]))

    def test_non_git_evidence_is_redacted_bounded_and_content_bound(self):
        root = self.workspace / "mirror"
        root.mkdir()
        source = root / "logic.py"
        source.write_text('secret="private-value"\n' + "line\n" * 4000)
        scope = {"projects": [str(root)], "evidence_files": [str(source)]}
        first = context.bundle(self.workspace, "Review mirror", [], [], scope)
        evidence = first["code_evidence"][0]
        self.assertNotIn("private-value", evidence["content"])
        self.assertTrue(evidence["truncated"])
        self.assertEqual(len(evidence["content"]), 12000)
        self.assertEqual(first["repository"]["projects"][0]["kind"], "directory")
        source.write_text(source.read_text() + "changed outside excerpt\n")
        second = context.bundle(self.workspace, "Review mirror", [], [], scope)
        self.assertNotEqual(first["repository"]["basis_hash"], second["repository"]["basis_hash"])

    def test_symlink_cannot_import_evidence_outside_projects(self):
        root = self.repo("first")
        external = self.root / "private.txt"
        external.write_text("private")
        link = root / "link.txt"
        link.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            context.bundle(
                self.workspace,
                "Review",
                [],
                [],
                {"projects": [str(root)], "evidence_files": [str(link)]},
            )

    def test_non_git_project_needs_explicit_evidence(self):
        root = self.workspace / "mirror"
        root.mkdir()
        with self.assertRaisesRegex(ValueError, "--evidence"):
            context.bundle(
                self.workspace, "Review", [], [], {"projects": [str(root)], "evidence_files": []}
            )

    def test_project_rules_and_evidence_order_are_visible(self):
        root = self.repo("first")
        (self.workspace / "AGENTS.md").write_text("Workspace rules")
        (root / "AGENTS.md").write_text("Project rules")
        value = context.bundle(
            self.workspace,
            "Review",
            [],
            [],
            {"projects": [str(root)], "evidence_files": [str(root / "logic.py")]},
        )
        self.assertEqual(
            [item["content"] for item in value["instructions"]],
            ["Workspace rules", "Project rules"],
        )
        self.assertIn("answer = 1", value["code_evidence"][0]["content"])
