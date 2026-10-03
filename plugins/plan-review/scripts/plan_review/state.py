"""Private, durable, session-scoped state with interprocess locking."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path


class StateError(RuntimeError):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def data_root() -> Path:
    if os.environ.get("PLAN_REVIEW_DATA_DIR"):
        return Path(os.environ["PLAN_REVIEW_DATA_DIR"]).expanduser()
    if os.environ.get("PLUGIN_DATA"):
        return Path(os.environ["PLUGIN_DATA"]) / "plan-review"
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return base / "codex-plan-review"


class Store:
    def __init__(self, root: Path, session: str, cwd: str):
        if not session or not cwd:
            raise StateError("session_id 和 cwd 不能为空")
        self.root = root
        self.key = digest(session + "\0" + str(Path(cwd).resolve()))
        self.path = root / (self.key + ".json")
        self.lock_path = root / (self.key + ".lock")

    @contextlib.contextmanager
    def locked(self) -> Iterator[Store]:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield self
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError("评审状态无法读取；请用 status 检查或显式 reset") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise StateError("评审状态格式不受支持；请用 status 检查或显式 reset")
        if value.get("status") not in {
            "idle",
            "reviewing",
            "needs_revision",
            "review_failed",
            "approved",
            "executing",
        }:
            raise StateError("评审状态无效")
        if not isinstance(value.get("requests", []), list) or not all(
            isinstance(item, str) for item in value.get("requests", [])
        ):
            raise StateError("评审请求记录无效")
        if value["status"] != "idle":
            if not isinstance(value.get("plan"), str) or not value["plan"]:
                raise StateError("评审计划内容缺失")
            if value.get("stored_plan_hash", value.get("plan_hash")) != digest(value["plan"]):
                raise StateError("计划内容与评审哈希不一致")
            if type(value.get("rounds")) is not int or value["rounds"] < 0:
                raise StateError("评审轮次无效")
            if not isinstance(value.get("context_digest"), str):
                raise StateError("评审依据缺失")
            if not isinstance(value.get("history", []), list):
                raise StateError("评审历史无效")
            if value["status"] in {"approved", "executing"} and (
                not isinstance(value.get("result"), dict)
                or value["result"].get("verdict") != "approve"
            ):
                raise StateError("批准状态缺少有效红队结果")
        return value

    def write(self, value: dict) -> None:
        value = {**value, "schema_version": 1}
        fd, name = tempfile.mkstemp(prefix=self.key + ".", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(value, out, ensure_ascii=False, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
