from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from modelmux.config import cache_home
from modelmux.errors import ModelMuxError
from modelmux.events import Event


ACTIVE_STATUSES = {"queued", "running"}
FINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    """Persistent metadata and lifecycle operations for ModelMux runs."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or cache_home() / "runs").expanduser().resolve()

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def _run_dir(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ModelMuxError(f"Invalid run id: {run_id}")
        return self.root / run_id

    def _metadata_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "meta.json"

    @contextmanager
    def _metadata_lock(self, run_id: str) -> Iterator[None]:
        path = self._run_dir(run_id) / ".metadata.lock"
        with path.open("a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read_unlocked(self, run_id: str) -> dict[str, Any]:
        path = self._metadata_path(run_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModelMuxError(f"Cannot read run {run_id}: {error}") from error
        if not isinstance(value, dict) or value.get("id") != run_id:
            raise ModelMuxError(f"Invalid metadata for run {run_id}")
        return value

    def _write_unlocked(self, record: dict[str, Any]) -> None:
        run_id = str(record["id"])
        destination = self._metadata_path(run_id)
        temporary = destination.with_name(f".meta.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                os.chmod(temporary, 0o600)
                json.dump(record, output, ensure_ascii=False, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def create(
        self,
        *,
        task: str,
        profile: str,
        extension: str,
        output_path: Path | None,
    ) -> tuple[dict[str, Any], BinaryIO]:
        self._ensure_root()
        run_id = uuid.uuid4().hex
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(mode=0o700)
        active = (run_dir / ".active.lock").open("a+b")
        try:
            os.fchmod(active.fileno(), 0o600)
            fcntl.flock(active, fcntl.LOCK_EX)
            now = _now()
            managed = output_path is None
            artifact = run_dir / f"artifact{extension}" if managed else output_path
            assert artifact is not None
            record: dict[str, Any] = {
                "schema_version": 1,
                "id": run_id,
                "name": run_id,
                "task": task,
                "profile": profile,
                "status": "queued",
                "progress": 0,
                "message": "Queued",
                "artifact": str(artifact.expanduser().resolve()),
                "managed_artifact": managed,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "pid": os.getpid(),
                "metadata": {},
            }
            self._write_unlocked(record)
            return record, active
        except BaseException:
            active.close()
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

    def get(self, run_id: str, *, reconcile: bool = True) -> dict[str, Any]:
        record = self._read_unlocked(run_id)
        if reconcile and record.get("status") in ACTIVE_STATUSES and not self.is_active(run_id):
            record = self.update(
                run_id,
                status="interrupted",
                message="Process exited unexpectedly",
                finished_at=_now(),
                pid=None,
            )
        return record

    def list(self) -> list[dict[str, Any]]:
        self._ensure_root()
        records: list[dict[str, Any]] = []
        for path in self.root.iterdir():
            if not path.is_dir() or not RUN_ID_PATTERN.fullmatch(path.name):
                continue
            try:
                records.append(self.get(path.name))
            except ModelMuxError:
                continue
        return sorted(records, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def update(self, run_id: str, **changes: Any) -> dict[str, Any]:
        with self._metadata_lock(run_id):
            record = self._read_unlocked(run_id)
            record.update(changes)
            record["updated_at"] = _now()
            self._write_unlocked(record)
            return record

    def record_event(self, run_id: str, event: Event) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if event.type == "progress":
            progress = int(event.data.get("progress", 0))
            changes["progress"] = max(0, min(100, progress))
        if event.data.get("message") is not None:
            changes["message"] = str(event.data["message"])
        if event.type == "result":
            changes["artifact"] = str(event.data.get("output", ""))
            metadata = event.data.get("metadata", {})
            changes["metadata"] = metadata if isinstance(metadata, dict) else {}
        if event.type == "error":
            changes["error"] = str(event.data.get("message", "Unknown error"))
        return self.update(run_id, **changes) if changes else self.get(run_id, reconcile=False)

    def finish(
        self,
        run_id: str,
        status: str,
        *,
        message: str,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in FINAL_STATUSES:
            raise ModelMuxError(f"Invalid final run status: {status}")
        changes: dict[str, Any] = {
            "status": status,
            "message": message,
            "error": error,
            "finished_at": _now(),
            "pid": None,
        }
        if status == "completed":
            changes["progress"] = 100
        if metadata is not None:
            changes["metadata"] = metadata
        return self.update(run_id, **changes)

    def is_active(self, run_id: str) -> bool:
        path = self._run_dir(run_id) / ".active.lock"
        try:
            with path.open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(lock, fcntl.LOCK_UN)
                return False
        except OSError:
            return False

    @contextmanager
    def queue_slot(self) -> Iterator[None]:
        self._ensure_root()
        with (self.root / ".queue.lock").open("a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def rename(self, run_id: str, name: str) -> dict[str, Any]:
        value = name.strip()
        if not value:
            raise ModelMuxError("Run name cannot be empty")
        if "\n" in value or "\r" in value:
            raise ModelMuxError("Run name must be one line")
        return self.update(run_id, name=value[:200])

    def delete(self, run_id: str) -> None:
        self.delete_many([run_id])

    def delete_many(self, run_ids: list[str]) -> None:
        run_dirs: list[Path] = []
        for run_id in run_ids:
            record = self.get(run_id)
            if record.get("status") in ACTIVE_STATUSES and self.is_active(run_id):
                raise ModelMuxError(f"Run {run_id} is active; cancel it first")
            run_dir = self._run_dir(run_id)
            if run_dir.is_symlink() or run_dir.resolve().parent != self.root:
                raise ModelMuxError(f"Unsafe run directory: {run_dir}")
            run_dirs.append(run_dir)
        for run_dir in run_dirs:
            shutil.rmtree(run_dir)

    def cancel(self, run_id: str) -> dict[str, Any]:
        record = self.get(run_id)
        if record.get("status") not in ACTIVE_STATUSES:
            raise ModelMuxError(f"Run {run_id} is not active")
        if not self.is_active(run_id):
            return self.get(run_id)
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            raise ModelMuxError(f"Run {run_id} has no valid process id")
        record = self.update(run_id, message="Cancelling")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return self.get(run_id)
        return record
