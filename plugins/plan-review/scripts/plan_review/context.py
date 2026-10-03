"""Build a bounded review bundle without depending on transcript formats."""

from __future__ import annotations

import hashlib
import json
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


MAX_PROJECTS = 4
MAX_EVIDENCE_FILES = 12
MAX_EVIDENCE_BYTES = 1_000_000
EVIDENCE_TEXT_BUDGET = 48000


def resolve_scope(
    cwd: Path, projects: list[str], evidence_files: list[str], explicit: bool | None = None
) -> dict:
    if not all(
        isinstance(items, list) and all(isinstance(item, str) and item for item in items)
        for items in (projects, evidence_files)
    ):
        raise ValueError("项目和证据文件列表格式无效")
    if len(projects) > MAX_PROJECTS or len(evidence_files) > MAX_EVIDENCE_FILES:
        raise ValueError("评审最多指定 4 个项目和 12 个证据文件")
    cwd = cwd.resolve()
    explicit = bool(projects) if explicit is None else explicit
    if type(explicit) is not bool:
        raise ValueError("项目范围标记无效")
    roots = []
    for name in projects or [str(cwd)]:
        path = Path(name).expanduser()
        path = (path if path.is_absolute() else cwd / path).resolve()
        if not path.is_dir():
            raise ValueError("--project 必须指向已存在的项目目录")
        roots.append(project_root(path))
    roots = list(dict.fromkeys(roots))
    if not explicit and not git(cwd, "rev-parse", "--show-toplevel").strip():
        # A workspace is not a repository. Never silently fingerprint an empty Git basis.
        if any(item.is_dir() and (item / ".git").exists() for item in cwd.iterdir()):
            raise ValueError("当前目录是多仓库工作区；请用 --project 分别指定目标项目")
    files = []
    for name in evidence_files:
        path = Path(name).expanduser()
        path = (path if path.is_absolute() else cwd / path).resolve()
        if not path.is_file() or not any(root in path.parents for root in roots):
            raise ValueError("--evidence 必须是目标项目内的已有文件，不能通过符号链接越界")
        if path.stat().st_size > MAX_EVIDENCE_BYTES:
            raise ValueError("单个证据文件超过 1 MB；请选择更小的关键源码文件")
        files.append(str(path))
    if explicit:
        for root in roots:
            if not git(root, "rev-parse", "--show-toplevel").strip() and not any(
                root in Path(name).parents for name in files
            ):
                raise ValueError("非 Git 源码目录必须用 --evidence 指定关键文件以绑定评审依据")
    return {
        "projects": [str(root) for root in roots],
        "evidence_files": list(dict.fromkeys(files)),
        "explicit": explicit,
    }


def repository_basis(root: Path) -> dict:
    head = git(root, "rev-parse", "HEAD").strip()
    changes = git(root, "diff", "--no-ext-diff", "--no-textconv", "--")
    changes += git(root, "diff", "--no-ext-diff", "--no-textconv", "--cached", "--")
    status = git(root, "status", "--porcelain=v1")
    untracked = hashlib.sha256()
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
    return {
        "path": str(root),
        "kind": "git" if git(root, "rev-parse", "--show-toplevel").strip() else "directory",
        "head": head,
        "status": redact(status[:4000]),
        "basis_hash": digest(head + "\0" + changes + "\0" + status + "\0" + untracked.hexdigest()),
    }


def bundle(
    cwd: Path, plan: str, requests: list[str], history: list[dict], scope: dict | None = None
) -> dict:
    if scope is not None and (
        not isinstance(scope, dict)
        or not {"projects", "evidence_files"} <= set(scope)
        or not set(scope) <= {"projects", "evidence_files", "explicit"}
    ):
        raise ValueError("评审项目范围格式无效")
    scope = resolve_scope(cwd, **(scope or {"projects": [], "evidence_files": []}))
    standards = {}
    budget = 32000
    for folder in [cwd, *(Path(root) for root in scope["projects"])]:
        for item in instructions(folder):
            if item["path"] not in standards:
                content = item["content"][:budget]
                standards[item["path"]] = {**item, "content": content}
                budget = max(0, budget - len(content))
    evidence = []
    budget = EVIDENCE_TEXT_BUDGET
    for name in scope["evidence_files"]:
        raw = Path(name).read_bytes()
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise ValueError("证据文件在读取前增大到 1 MB 以上；未启动评审")
        text = redact(raw.decode("utf-8"))
        limit = min(12000, budget)
        evidence.append(
            {
                "path": name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content": text[:limit],
                "truncated": len(text) > limit,
            }
        )
        budget -= min(len(text), limit)
    repositories = [repository_basis(Path(root)) for root in scope["projects"]]
    basis = digest(
        json.dumps(
            [repositories, list(standards.values()), evidence], ensure_ascii=False, sort_keys=True
        )
    )
    return {
        "plan": redact(plan),
        "requests": [redact(text) for text in requests],
        "instructions": list(standards.values()),
        "scope": scope,
        "code_evidence": evidence,
        "repository": {"projects": repositories, "basis_hash": basis},
        "previous_reviews": history[-3:],
    }
