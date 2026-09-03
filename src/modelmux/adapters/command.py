from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import modelmux

from modelmux.adapters.base import Adapter, RunContext, RunResult
from modelmux.errors import ModelMuxError
from modelmux.events import Event


class StrictValues(dict[str, Any]):
    def __missing__(self, key: str) -> Any:
        raise ModelMuxError(f"Command template requires parameter {key!r}")


class CommandAdapter(Adapter):
    """Run a one-shot command or reuse a profile's persistent worker process."""

    SAFE_ENVIRONMENT = {
        "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "SHELL",
        "TMPDIR", "USER",
    }

    def __init__(self, profile) -> None:
        super().__init__(profile)
        self._worker: subprocess.Popen[str] | None = None
        self._diagnostics: list[str] = []

    def _command(self) -> dict[str, Any]:
        command = self.profile.data.get("command", {})
        if not isinstance(command, dict):
            raise ModelMuxError(f"Profile {self.profile.name!r} requires command")
        return command

    def _values(self, context: RunContext | None = None) -> StrictValues:
        values: dict[str, Any] = {
            "package_root": str(Path(modelmux.__file__).resolve().parent),
            "task": self.profile.task,
            "profile": self.profile.name,
            **self.profile.defaults,
        }
        if context is not None:
            values.update(
                input_path=str(context.input_path),
                output_path=str(context.output_path),
                **context.parameters,
            )
        return StrictValues(values)

    def _argv(self, key: str, context: RunContext | None = None) -> list[str]:
        template = self._command().get(key)
        if not isinstance(template, list) or not template:
            raise ModelMuxError(f"Profile {self.profile.name!r} requires command.{key}")
        try:
            return [
                os.path.expanduser(str(value).format_map(self._values(context)))
                for value in template
            ]
        except (KeyError, ValueError) as error:
            raise ModelMuxError(f"Invalid command template: {error}") from error

    def _environment(self, context: RunContext | None = None) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items()
                       if key in self.SAFE_ENVIRONMENT}
        configured = self._command().get("env", {})
        if configured:
            if not isinstance(configured, dict):
                raise ModelMuxError("command.env must be a mapping")
            values = self._values(context)
            environment.update({str(key): str(value).format_map(values)
                                for key, value in configured.items()})
        return environment

    @staticmethod
    def _check_executable(argv: list[str]) -> None:
        executable = Path(argv[0]).expanduser()
        if executable.is_absolute() and not executable.is_file():
            raise ModelMuxError(f"Command does not exist: {executable}")

    def load(self) -> None:
        if "worker_argv" not in self._command():
            return
        if self._worker is not None and self._worker.poll() is None:
            return
        self._worker = None
        argv = self._argv("worker_argv")
        self._check_executable(argv)
        try:
            worker = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self._environment(),
            )
        except OSError as error:
            raise ModelMuxError(f"Cannot start worker {argv[0]}: {error}") from error
        self._worker = worker
        self._diagnostics = []

        def drain_stderr() -> None:
            assert worker.stderr is not None
            for line in worker.stderr:
                self._diagnostics.append(line.rstrip())
                del self._diagnostics[:-100]

        threading.Thread(target=drain_stderr, daemon=True).start()
        assert worker.stdout is not None
        for line in worker.stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                self._diagnostics.append(line.rstrip())
                continue
            if isinstance(payload, dict) and payload.get("type") == "ready":
                return
            if isinstance(payload, dict) and payload.get("type") == "error":
                self.close()
                raise ModelMuxError(str(payload.get("message", "Worker failed to load")))
        worker.wait()
        detail = "\n".join(self._diagnostics[-10:])
        self.close()
        raise ModelMuxError(f"Worker exited before ready: {detail or worker.returncode}")

    def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None or worker.poll() is not None:
            return
        try:
            assert worker.stdin is not None
            worker.stdin.write('{"type":"shutdown"}\n')
            worker.stdin.flush()
            worker.wait(timeout=5)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()

    def run(self, context: RunContext) -> RunResult:
        if self._worker is not None:
            return self._run_worker(context)
        return self._run_once(context)

    def _run_once(self, context: RunContext) -> RunResult:
        command = self._command()
        argv = self._argv("argv", context)
        self._check_executable(argv)
        context.output_path.parent.mkdir(parents=True, exist_ok=True)
        context.emit(Event("progress", {"progress": 0,
                                        "message": f"Running {self.profile.name}…"}))
        process: subprocess.Popen[str] | None = None
        watcher_done: threading.Event | None = None
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._environment(context),
            )
            watcher_done = self._watch_cancellation(process, context)
            if command.get("events") == "jsonl":
                stdout, stderr = self._stream_events(process, context)
            else:
                stdout, stderr = process.communicate()
        except KeyboardInterrupt:
            if process is not None:
                process.terminate()
                process.wait()
            raise
        except OSError as error:
            raise ModelMuxError(f"Cannot run {argv[0]}: {error}") from error
        finally:
            if watcher_done is not None:
                watcher_done.set()
        if context.cancelled is not None and context.cancelled.is_set():
            raise KeyboardInterrupt
        if process.returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"exit status {process.returncode}"
            raise ModelMuxError(f"Adapter command failed: {detail}")
        return self._result(context)

    def _run_worker(self, context: RunContext) -> RunResult:
        worker = self._worker
        assert worker is not None and worker.stdin is not None and worker.stdout is not None
        context.output_path.parent.mkdir(parents=True, exist_ok=True)
        request = {
            "type": "run",
            "input_path": str(context.input_path),
            "output_path": str(context.output_path),
            "parameters": context.parameters,
        }
        watcher_done: threading.Event | None = None
        try:
            worker.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            worker.stdin.flush()
            watcher_done = self._watch_cancellation(worker, context)
            for line in worker.stdout:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    self._diagnostics.append(line.rstrip())
                    continue
                if not isinstance(payload, dict):
                    continue
                kind = payload.pop("type", None)
                if kind == "progress":
                    context.emit(Event("progress", payload))
                elif kind == "result":
                    return self._result(context)
                elif kind == "error":
                    raise ModelMuxError(str(payload.get("message", "Worker failed")))
        except (BrokenPipeError, OSError) as error:
            detail = "\n".join(self._diagnostics[-10:])
            raise ModelMuxError(f"Worker protocol failed: {detail or error}") from error
        finally:
            if watcher_done is not None:
                watcher_done.set()
            if worker.poll() is not None:
                self._worker = None
        if context.cancelled is not None and context.cancelled.is_set():
            raise KeyboardInterrupt
        raise ModelMuxError("Worker exited without a result")

    @staticmethod
    def _watch_cancellation(
        process: subprocess.Popen[str], context: RunContext
    ) -> threading.Event | None:
        cancelled = context.cancelled
        if cancelled is None:
            return None
        done = threading.Event()

        def terminate_when_cancelled() -> None:
            while process.poll() is None and not done.wait(0.1):
                if cancelled.is_set():
                    process.terminate()
                    return

        threading.Thread(target=terminate_when_cancelled, daemon=True).start()
        return done

    def _result(self, context: RunContext) -> RunResult:
        if context.cancelled is not None and context.cancelled.is_set():
            raise KeyboardInterrupt
        if not context.output_path.is_file():
            raise ModelMuxError(f"Adapter did not create output: {context.output_path}")
        context.emit(Event("progress", {"progress": 100, "message": "Done"}))
        return RunResult(context.output_path, {"adapter": "command"})

    @staticmethod
    def _stream_events(
        process: subprocess.Popen[str], context: RunContext
    ) -> tuple[str, str]:
        stdout_parts: list[str] = []
        diagnostics: list[str] = []

        def read_stdout() -> None:
            assert process.stdout is not None
            stdout_parts.append(process.stdout.read())

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        assert process.stderr is not None
        for line in process.stderr:
            value = line.strip()
            if not value:
                continue
            try:
                payload = json.loads(value)
            except json.JSONDecodeError:
                diagnostics.append(value)
                continue
            if isinstance(payload, dict) and isinstance(payload.get("type"), str):
                kind = str(payload.pop("type"))
                context.emit(Event(kind, payload))
            else:
                diagnostics.append(value)
        process.wait()
        reader.join()
        return "".join(stdout_parts), "\n".join(diagnostics)
