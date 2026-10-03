"""Recognize a literal plan submission without executing arbitrary shell code."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "review.py"


def submit_command() -> str:
    return f"python3 {shlex.quote(str(SCRIPT))} submit --stdin"


def parse_submission(command: object) -> dict | None:
    if not isinstance(command, str):
        return None
    match = re.fullmatch(
        r"(?P<head>[^\n\r]+?)\s+<<'(?P<label>[A-Za-z_][A-Za-z0-9_]*)'\r?\n"
        r"(?P<body>[\s\S]*)\r?\n(?P=label)\r?\n?",
        command,
    )
    if match is None:
        return None
    try:
        argv = shlex.split(match["head"])
    except ValueError:
        return None
    if len(argv) < 4 or Path(argv[0]).name != "python3":
        return None
    if argv[2:4] != ["submit", "--stdin"]:
        return None
    if Path(argv[1]).resolve() != SCRIPT:
        return None
    if any(line.rstrip("\r") == match["label"] for line in match["body"].splitlines()):
        return None
    scope = {"projects": [], "evidence_files": []}
    extra = argv[4:]
    if len(extra) % 2:
        return None
    for option, value in zip(extra[::2], extra[1::2]):
        if option not in {"--project", "--evidence"} or not value or value.startswith("-"):
            return None
        scope["projects" if option == "--project" else "evidence_files"].append(value)
    return {"plan": match["body"], "scope": scope}


def extract_submission(command: object) -> str | None:
    request = parse_submission(command)
    return request["plan"] if request else None


def output_command(result: dict) -> str:
    # shlex.quote is shell escaping; JSON serialization alone is not.
    return "printf '%s\\n' " + shlex.quote(json.dumps(result, ensure_ascii=False))


def recovery_command(command: object) -> str | None:
    if not isinstance(command, str) or "\n" in command:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) != 3 or Path(argv[0]).name != "python3":
        return None
    if Path(argv[1]).resolve() != SCRIPT or argv[2] not in {"status", "plan", "retry", "reset"}:
        return None
    return argv[2]
