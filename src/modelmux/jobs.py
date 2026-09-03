from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from modelmux.adapters import Adapter, load_adapter
from modelmux.config import Profile, ProfileStore, ServerSettings
from modelmux.errors import ModelMuxError
from modelmux.events import null_sink
from modelmux.runs import ACTIVE_STATUSES, FINAL_STATUSES, RunStore
from modelmux.runtime import execute_created_run


@dataclass
class JobControl:
    cancelled: threading.Event
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

    def _resident_adapter(self, profile: Profile) -> Adapter:
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
            adapter.load()
            return adapter

    def _adapter_for(self, profile: Profile) -> tuple[Adapter, threading.Lock | None]:
        if self.settings.model_loading == "ephemeral":
            return load_adapter(profile), None
        adapter = self._resident_adapter(profile)
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
        input_path = self.runs.input_path(run_id, profile.input_extension)
        try:
            stage(input_path)
            input_path.chmod(0o600)
        except BaseException:
            input_path.unlink(missing_ok=True)
            self.runs.finish(run_id, "failed", message="Failed to stage input")
            active_lock.close()
            raise
        control = JobControl(threading.Event())
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
            adapter, adapter_lock = self._adapter_for(profile)
            if adapter_lock is None:
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
            else:
                with adapter_lock:
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

    def cancel(self, run_id: str) -> dict[str, Any]:
        return self.cancel_many([run_id])[0]

    def cancel_many(self, run_ids: list[str]) -> list[dict[str, Any]]:
        controls: list[JobControl] = []
        with self._lock:
            for run_id in run_ids:
                record = self.runs.get(run_id)
                if record.get("status") not in ACTIVE_STATUSES:
                    raise ModelMuxError(f"Run {run_id} is not active")
                control = self._controls.get(run_id)
                if control is None:
                    raise ModelMuxError(f"Run {run_id} is not owned by this server")
                controls.append(control)
        for control in controls:
            control.cancelled.set()
        return [self.runs.update(run_id, message="Cancelling") for run_id in run_ids]

    def wait(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            record = self.runs.get(run_id, reconcile=False)
            if record.get("status") in FINAL_STATUSES:
                return record
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(run_id)
            time.sleep(0.05)

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
