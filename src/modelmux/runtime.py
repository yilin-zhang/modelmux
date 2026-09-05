from __future__ import annotations

import fcntl
import threading
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any, BinaryIO

from modelmux.adapters import Adapter, RunContext, RunResult
from modelmux.config import Profile
from modelmux.events import Event, EventSink
from modelmux.runs import RunStore


def execute_created_run(
    *,
    store: RunStore,
    record: dict[str, Any],
    active_lock: BinaryIO,
    task: str,
    profile: Profile,
    input_path: Path,
    parameters: dict[str, Any],
    emit: EventSink,
    cancelled: threading.Event | None = None,
    adapter: Adapter,
) -> RunResult:
    """Execute a run created by ``RunStore.create`` and finalize its record."""
    run_id = str(record["id"])
    destination = Path(str(record["artifact"]))
    managed_output = bool(record["managed_artifact"])

    def tracked_emit(event: Event) -> None:
        event = Event(event.type, {**event.data, "id": run_id})
        store.record_event(run_id, event)
        try:
            emit(event)
        except BrokenPipeError:
            pass

    try:
        tracked_emit(
            Event(
                "queued",
                {
                    "task": task,
                    "profile": profile.name,
                    "name": run_id,
                    "message": "Queued",
                },
            )
        )
        if cancelled is not None and cancelled.is_set():
            raise KeyboardInterrupt
        store.update(
            run_id,
            status="running",
            message=f"Loading {profile.name}…",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        tracked_emit(
            Event(
                "started",
                {
                    "task": task,
                    "profile": profile.name,
                    "message": f"Loading {profile.name}…",
                },
            )
        )
        context = RunContext(
            task=task,
            profile=profile,
            input_path=input_path,
            output_path=destination,
            parameters=parameters,
            emit=tracked_emit,
            cancelled=cancelled,
        )
        result = adapter.run(context)
        if cancelled is not None and cancelled.is_set():
            raise KeyboardInterrupt
        if managed_output:
            result.output_path.parent.chmod(0o700)
            result.output_path.chmod(0o600)
        store.finish(run_id, "completed", message="Completed", metadata=result.metadata)
        tracked_emit(
            Event(
                "result",
                {
                    "task": task,
                    "profile": profile.name,
                    "output": str(result.output_path),
                    "metadata": result.metadata,
                    "message": "Completed",
                },
            )
        )
        return RunResult(result.output_path, result.metadata, run_id)
    except KeyboardInterrupt:
        store.finish(run_id, "cancelled", message="Cancelled")
        raise
    except Exception as error:
        store.finish(run_id, "failed", message="Failed", error=str(error))
        raise
    finally:
        input_path.unlink(missing_ok=True)
        fcntl.flock(active_lock, fcntl.LOCK_UN)
        active_lock.close()


def stage_input(
    store: RunStore,
    run_id: str,
    extension: str,
    active_lock: BinaryIO,
    stage: Callable[[Path], object],
) -> Path:
    """Write a run's input privately, failing the run and releasing its lock on error."""
    input_path = store.input_path(run_id, extension)
    try:
        stage(input_path)
        input_path.chmod(0o600)
    except BaseException:
        input_path.unlink(missing_ok=True)
        store.finish(run_id, "failed", message="Failed to stage input")
        active_lock.close()
        raise
    return input_path
