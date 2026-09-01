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
    """Run an argv array without a shell. Suitable for CLI-backed models."""

    SAFE_ENVIRONMENT = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TMPDIR",
        "USER",
    }

    def run(self, context: RunContext) -> RunResult:
        command = self.profile.data.get("command", {})
        argv_template = command.get("argv") if isinstance(command, dict) else None
        if not isinstance(argv_template, list) or not argv_template:
            raise ModelMuxError(f"Profile {self.profile.name!r} requires command.argv")
        values = StrictValues(
            input_path=str(context.input_path),
            output_path=str(context.output_path),
            package_root=str(Path(modelmux.__file__).resolve().parent),
            task=context.task,
            profile=context.profile.name,
            **context.parameters,
        )
        try:
            argv = [os.path.expanduser(str(value).format_map(values)) for value in argv_template]
        except (KeyError, ValueError) as error:
            raise ModelMuxError(f"Invalid command template: {error}") from error

        executable = Path(argv[0]).expanduser()
        if executable.is_absolute() and not executable.is_file():
            raise ModelMuxError(f"Command does not exist: {executable}")
        context.output_path.parent.mkdir(parents=True, exist_ok=True)
        context.emit(Event("progress", {"progress": 0, "message": f"Running {self.profile.name}…"}))
        # Profiles are executable configuration. Do not also expose unrelated
        # credentials from the parent process to every model command.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in self.SAFE_ENVIRONMENT
        }
        configured_env = command.get("env", {})
        if configured_env:
            if not isinstance(configured_env, dict):
                raise ModelMuxError("command.env must be a mapping")
            environment.update({str(key): str(value).format_map(values) for key, value in configured_env.items()})
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            if command.get("events") == "jsonl":
                stdout, stderr = self._stream_events(process, context)
            else:
                stdout, stderr = process.communicate()
        except KeyboardInterrupt:
            process.terminate()
            process.wait()
            raise
        except OSError as error:
            raise ModelMuxError(f"Cannot run {argv[0]}: {error}") from error
        if process.returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"exit status {process.returncode}"
            raise ModelMuxError(f"Adapter command failed: {detail}")
        if not context.output_path.is_file():
            raise ModelMuxError(f"Adapter did not create output: {context.output_path}")
        context.emit(Event("progress", {"progress": 100, "message": "Done"}))
        return RunResult(
            output_path=context.output_path,
            metadata={"adapter": "command", "command": argv[0]},
        )

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
