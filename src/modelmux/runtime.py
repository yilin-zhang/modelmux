from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any

from modelmux.adapters import RunContext, RunResult, load_adapter
from modelmux.config import Profile, cache_home
from modelmux.errors import ModelMuxError
from modelmux.events import Event, EventSink


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
    managed_output = output_path is None
    destination = output_path or (
        cache_home() / "runs" / f"{uuid.uuid4().hex}{profile.extension}"
    )
    destination = destination.expanduser().resolve()
    input_config = profile.data.get("input", {})
    suffix = str(input_config.get("extension", ".input")) if isinstance(input_config, dict) else ".input"
    emit(Event("started", {"task": task, "profile": profile.name, "message": f"Loading {profile.name}…"}))
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
            emit=emit,
        )
        result = load_adapter(profile).run(context)
    if managed_output:
        result.output_path.parent.chmod(0o700)
        result.output_path.chmod(0o600)
    emit(
        Event(
            "result",
            {
                "task": task,
                "profile": profile.name,
                "output": str(result.output_path),
                "metadata": result.metadata,
                "message": str(result.output_path),
            },
        )
    )
    return result
