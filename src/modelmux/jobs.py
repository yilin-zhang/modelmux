from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable

from modelmux.adapters import Adapter, load_adapter
from modelmux.config import Profile, ProfileStore, ServerSettings
from modelmux.errors import ModelMuxError
from modelmux.events import null_sink
from modelmux.runs import ACTIVE_STATUSES, FINAL_STATUSES, RunStore
from modelmux.runtime import execute_created_run, stage_input


@dataclass
class JobControl:
    cancelled: threading.Event
    input_path: Path
    active_lock: BinaryIO
    finished: threading.Event = field(default_factory=threading.Event)
    future: Future[None] | None = None


class JobManager:
    """Own the server-side queue, adapters, cancellation, and run records."""

    def __init__(
        self,
        settings: ServerSettings,
        *,
        profiles: ProfileStore | None = None,
        runs: RunStore | None = None,
    ) -> None:
        self.settings = settings
        self.profiles = profiles or ProfileStore()
        self.runs = runs or RunStore()
        self._executor = ThreadPoolExecutor(
            max_workers=settings.concurrency,
            thread_name_prefix="modelmux-worker",
        )
        self._lock = threading.RLock()
        self._adapter_cache_lock = threading.Lock()
        self._controls: dict[str, JobControl] = {}
        self._adapters: dict[str, Adapter] = {}
        self._adapter_locks: dict[str, threading.Lock] = {}
        self._stopping = False
        if settings.model_loading == "preload":
            for name in settings.preload:
                self._resident_adapter(self.profiles.get(name))

    def _resident_adapter(
        self, profile: Profile, cancelled: threading.Event | None = None
    ) -> Adapter:
        with self._adapter_cache_lock:
            adapter = self._adapters.get(profile.name)
            if adapter is None:
                if self.settings.model_loading == "lazy" and self.settings.concurrency == 1:
                    for resident in self._adapters.values():
                        resident.close()
                    self._adapters.clear()
                    self._adapter_locks.clear()
                adapter = load_adapter(profile)
                self._adapters[profile.name] = adapter
                self._adapter_locks[profile.name] = threading.Lock()
            adapter.load(cancelled)
            return adapter

    def _adapter_for(
        self, profile: Profile, cancelled: threading.Event | None = None
    ) -> tuple[Adapter, threading.Lock | None]:
        if self.settings.model_loading == "ephemeral":
            return load_adapter(profile), None
        adapter = self._resident_adapter(profile, cancelled)
        return adapter, self._adapter_locks[profile.name]

    def submit(
        self,
        *,
        task: str,
        profile_name: str | None,
        input_bytes: bytes,
        parameters: dict[str, Any] | None = None,
        output_path: Path | None = None,
    ) -> dict[str, Any]:
        return self._submit(
            task=task,
            profile_name=profile_name,
            parameters=parameters,
            output_path=output_path,
            stage=lambda path: path.write_bytes(input_bytes),
        )

    def submit_file(
        self,
        *,
        task: str,
        profile_name: str | None,
        source: Path,
        parameters: dict[str, Any] | None = None,
        output_path: Path | None = None,
    ) -> dict[str, Any]:
        """Submit a staged file without retaining its contents in memory."""
        return self._submit(
            task=task,
            profile_name=profile_name,
            parameters=parameters,
            output_path=output_path,
            stage=lambda path: os.replace(source, path),
        )

    def _submit(
        self,
        *,
        task: str,
        profile_name: str | None,
        parameters: dict[str, Any] | None,
        output_path: Path | None,
        stage: Callable[[Path], object],
    ) -> dict[str, Any]:
        with self._lock:
            if self._stopping:
                raise ModelMuxError("ModelMux server is stopping")
        name = profile_name or self.profiles.default_for(task)
        if not name:
            raise ModelMuxError(f"No default {task!r} profile; pass model")
        profile = self.profiles.get(name)
        if profile.task != task:
            raise ModelMuxError(
                f"Profile {profile.name!r} handles {profile.task!r}, not {task!r}"
            )
        merged = dict(profile.defaults)
        if parameters:
            merged.update(parameters)
        record, active_lock = self.runs.create(
            task=task,
            profile=profile.name,
            extension=profile.extension,
            output_path=output_path,
        )
        run_id = str(record["id"])
        input_path = stage_input(
            self.runs, run_id, profile.input_extension, active_lock, stage
        )
        control = JobControl(threading.Event(), input_path, active_lock)
        with self._lock:
            if self._stopping:
                input_path.unlink(missing_ok=True)
                self.runs.finish(run_id, "cancelled", message="Server is stopping")
                active_lock.close()
                raise ModelMuxError("ModelMux server is stopping")
            self._controls[run_id] = control
            try:
                control.future = self._executor.submit(
                    self._execute,
                    record,
                    active_lock,
                    profile,
                    input_path,
                    merged,
                    control,
                )
            except RuntimeError as error:
                self._controls.pop(run_id, None)
                input_path.unlink(missing_ok=True)
                self.runs.finish(run_id, "cancelled", message="Server is stopping")
                active_lock.close()
                raise ModelMuxError("ModelMux server is stopping") from error
        return self.runs.get(run_id, reconcile=False)

    def _execute(
        self,
        record: dict[str, Any],
        active_lock,
        profile: Profile,
        input_path: Path,
        parameters: dict[str, Any],
        control: JobControl,
    ) -> None:
        adapter: Adapter | None = None
        adapter_lock: threading.Lock | None = None
        execution_started = False
        try:
            if control.cancelled.is_set():
                self.runs.finish(str(record["id"]), "cancelled", message="Cancelled")
                return
            adapter, adapter_lock = self._adapter_for(profile, control.cancelled)
            with adapter_lock or nullcontext():
                execution_started = True
                execute_created_run(
                    store=self.runs,
                    record=record,
                    active_lock=active_lock,
                    task=profile.task,
                    profile=profile,
                    input_path=input_path,
                    parameters=parameters,
                    emit=null_sink,
                    cancelled=control.cancelled,
                    adapter=adapter,
                )
        except KeyboardInterrupt:
            if not execution_started:
                self.runs.finish(str(record["id"]), "cancelled", message="Cancelled")
        except Exception as error:
            if not execution_started:
                self.runs.finish(
                    str(record["id"]), "failed", message="Failed", error=str(error)
                )
        finally:
            input_path.unlink(missing_ok=True)
            if not execution_started:
                active_lock.close()
            if self.settings.model_loading == "ephemeral" and adapter is not None:
                adapter.close()
            with self._lock:
                self._controls.pop(str(record["id"]), None)
            control.finished.set()

    def cancel(self, run_id: str) -> dict[str, Any]:
        return self.cancel_many([run_id])[0]

    def cancel_many(self, run_ids: list[str]) -> list[dict[str, Any]]:
        controls: list[tuple[str, JobControl]] = []
        with self._lock:
            for run_id in run_ids:
                record = self.runs.get(run_id)
                if record.get("status") not in ACTIVE_STATUSES:
                    raise ModelMuxError(f"Run {run_id} is not active")
                control = self._controls.get(run_id)
                if control is None:
                    raise ModelMuxError(f"Run {run_id} is not owned by this server")
                controls.append((run_id, control))
        results: list[dict[str, Any]] = []
        for run_id, control in controls:
            control.cancelled.set()
            if control.future is not None and control.future.cancel():
                control.input_path.unlink(missing_ok=True)
                result = self.runs.finish(run_id, "cancelled", message="Cancelled")
                control.active_lock.close()
                with self._lock:
                    self._controls.pop(run_id, None)
                control.finished.set()
            else:
                result = self.runs.update(run_id, message="Cancelling")
            results.append(result)
        return results

    def wait(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until a run this server owns reaches a final status."""
        with self._lock:
            control = self._controls.get(run_id)
        record = self.runs.get(run_id, reconcile=False)
        if control is None or record.get("status") in FINAL_STATUSES:
            return record
        if not control.finished.wait(timeout):
            raise TimeoutError(run_id)
        return self.runs.get(run_id, reconcile=False)

    def shutdown(self) -> None:
        with self._lock:
            self._stopping = True
            controls = list(self._controls.values())
        for control in controls:
            control.cancelled.set()
        self._executor.shutdown(wait=True, cancel_futures=False)
        with self._adapter_cache_lock:
            adapters = list(self._adapters.values())
            self._adapters.clear()
            self._adapter_locks.clear()
        for adapter in adapters:
            adapter.close()
