"""Build a bounded review bundle without depending on transcript formats."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tomllib
from pathlib import Path

from .state import digest


def redact(text: str) -> str:
    text = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
        flags=re.S,
    )
    text = re.sub(r"\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{20,})\b", "[REDACTED]", text)
    text = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
    return re.sub(
        r"(?im)([\"']?(?:api[_-]?key|password|secret|access[_-]?token|token)"
        r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\n,\s]+)",
        r"\1[REDACTED]",
        text,
    )


def git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=3
        )
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def project_root(cwd: Path) -> Path:
    root = git(cwd, "rev-parse", "--show-toplevel").strip()
    return Path(root).resolve() if root else cwd.resolve()


def read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"无法读取 Codex 配置：{path.name}") from exc


def config_layers(cwd: Path) -> list[dict]:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    layers = [read_config(home / "config.toml")]
    root = project_root(cwd)
    paths = [root, *reversed(cwd.resolve().parents)]
    for parent in dict.fromkeys(paths):
        if parent == root or root in parent.parents:
            layers.append(read_config(parent / ".codex/config.toml"))
    if cwd.resolve() != root:
        layers.append(read_config(cwd / ".codex/config.toml"))
    return layers


def instructions(cwd: Path) -> list[dict]:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    layers = config_layers(cwd)
    fallbacks: list[str] = []
    for layer in layers:
        fallbacks = layer.get("project_doc_fallback_filenames", fallbacks)
    root = project_root(cwd)
    folders = [home, root]
    current = cwd.resolve()
    inside = []
    while current != root and root in current.parents:
        inside.append(current)
        current = current.parent
    folders.extend(reversed(inside))
    result = []
    budget = 32000
    for folder in dict.fromkeys(folders):
        for name in ["AGENTS.override.md", "AGENTS.md", *fallbacks]:
            if not isinstance(name, str) or Path(name).name != name:
                continue
            path = folder / name
            if path.is_file():
                content = redact(path.read_text(encoding="utf-8"))
                result.append({"path": str(path), "content": content[:budget]})
                budget = max(0, budget - len(content))
                break
    return result


def bundle(cwd: Path, plan: str, requests: list[str], history: list[dict]) -> dict:
    standards = instructions(cwd)
    head = git(cwd, "rev-parse", "HEAD").strip()
    changes = git(cwd, "diff", "--no-ext-diff", "--no-textconv", "--")
    changes += git(cwd, "diff", "--no-ext-diff", "--no-textconv", "--cached", "--")
    status = git(cwd, "status", "--porcelain=v1")
    untracked = hashlib.sha256()
    root = project_root(cwd)
    for name in sorted(git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")):
        if not name:
            continue
        path = root / name
        untracked.update(name.encode("utf-8"))
        if path.is_symlink():
            untracked.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    untracked.update(chunk)
        untracked.update(b"\0")
    basis = digest(
        head + "\0" + changes + "\0" + status + "\0" + str(standards) + "\0" + untracked.hexdigest()
    )
    return {
        "plan": redact(plan),
        "requests": [redact(text) for text in requests],
        "instructions": standards,
        "repository": {"head": head, "status": status[:4000], "basis_hash": basis},
        "previous_reviews": history[-3:],
    }
