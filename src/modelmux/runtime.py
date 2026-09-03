from __future__ import annotations

import fcntl
import signal
import tempfile
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Generator

from modelmux.adapters import Adapter, RunContext, RunResult, load_adapter
from modelmux.config import Profile, cache_home
from modelmux.errors import ModelMuxError
from modelmux.events import Event, EventSink
from modelmux.runs import RunStore


@contextmanager
def _cancel_on_sigterm() -> Generator[None, None, None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def cancel(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def execute_created_run(
    *,
    store: RunStore,
    record: dict[str, Any],
    active_lock: BinaryIO,
    task: str,
    profile: Profile,
    input_bytes: bytes,
    parameters: dict[str, Any],
    emit: EventSink,
    cancelled: threading.Event | None = None,
    serialize: bool = False,
    adapter: Adapter | None = None,
) -> RunResult:
    """Execute a run created by ``RunStore.create`` and finalize its record."""
    run_id = str(record["id"])
    destination = Path(str(record["artifact"]))
    managed_output = bool(record["managed_artifact"])
    input_config = profile.data.get("input", {})
    suffix = (
        str(input_config.get("extension", ".input"))
        if isinstance(input_config, dict)
        else ".input"
    )

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
        queue = store.queue_slot() if serialize else nullcontext()
        with _cancel_on_sigterm(), queue:
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
            scratch_root = cache_home() / "tmp"
            scratch_root.mkdir(parents=True, exist_ok=True)
            scratch_root.chmod(0o700)
            with tempfile.TemporaryDirectory(prefix="run-", dir=scratch_root) as temporary:
                input_path = Path(temporary) / f"input{suffix}"
                input_path.write_bytes(input_bytes)
                context = RunContext(
                    task=task,
                    profile=profile,
                    input_path=input_path,
                    output_path=destination,
                    parameters=parameters,
                    emit=tracked_emit,
                    cancelled=cancelled,
                )
                result = (adapter or load_adapter(profile)).run(context)
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
        fcntl.flock(active_lock, fcntl.LOCK_UN)
        active_lock.close()


def run_profile(
    *,
    task: str,
    profile: Profile,
    input_bytes: bytes,
    output_path: Path | None,
    parameters: dict[str, Any],
    emit: EventSink,
) -> RunResult:
    if task != profile.task:
        raise ModelMuxError(
            f"Profile {profile.name!r} handles {profile.task!r}, not {task!r}"
        )
    store = RunStore()
    record, active_lock = store.create(
        task=task,
        profile=profile.name,
        extension=profile.extension,
        output_path=output_path,
    )
    return execute_created_run(
        store=store,
        record=record,
        active_lock=active_lock,
        task=task,
        profile=profile,
        input_bytes=input_bytes,
        parameters=parameters,
        emit=emit,
        serialize=True,
    )
