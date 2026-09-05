from pathlib import Path
from typing import Any

import pytest

from modelmux.adapters import load_adapter
from modelmux.adapters.base import RunResult
from modelmux.config import Profile
from modelmux.events import EventSink, null_sink
from modelmux.runs import RunStore
from modelmux.runtime import execute_created_run, stage_input


def make_profile(name: str, **data) -> Profile:
    """Build a Profile literal for tests, with the common fields filled in."""
    return Profile(
        name,
        "test",
        {
            "task": "copy",
            "adapter": "copy",
            "defaults": {},
            "input": {"extension": ".txt"},
            "output": {"extension": ".txt"},
            **data,
        },
    )


@pytest.fixture
def copy_profile() -> Profile:
    return make_profile("copy-test")


@pytest.fixture
def cache(tmp_path: Path, monkeypatch) -> Path:
    """Point ModelMux's cache at a temporary directory."""
    root = tmp_path / "cache"
    monkeypatch.setenv("MODELMUX_CACHE_HOME", str(root))
    return root


def execute_profile(
    profile: Profile,
    input_bytes: bytes,
    *,
    parameters: dict[str, Any] | None = None,
    emit: EventSink = null_sink,
) -> RunResult:
    """Exercise the production run lifecycle with a concrete test profile."""
    store = RunStore()
    record, active_lock = store.create(
        task=profile.task,
        profile=profile.name,
        extension=profile.extension,
        output_path=None,
    )
    input_path = stage_input(
        store,
        str(record["id"]),
        profile.input_extension,
        active_lock,
        lambda path: path.write_bytes(input_bytes),
    )
    adapter = load_adapter(profile)
    try:
        return execute_created_run(
            store=store,
            record=record,
            active_lock=active_lock,
            task=profile.task,
            profile=profile,
            input_path=input_path,
            parameters=parameters or {},
            emit=emit,
            adapter=adapter,
        )
    finally:
        adapter.close()
