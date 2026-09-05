from __future__ import annotations

import importlib
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modelmux.config import Profile
from modelmux.errors import ModelMuxError
from modelmux.events import EventSink


@dataclass(frozen=True)
class RunContext:
    task: str
    profile: Profile
    input_path: Path
    output_path: Path
    parameters: dict[str, Any]
    emit: EventSink
    cancelled: threading.Event | None = None


@dataclass(frozen=True)
class RunResult:
    output_path: Path
    metadata: dict[str, Any]
    run_id: str | None = None


class Adapter(ABC):
    def __init__(self, profile: Profile) -> None:
        self.profile = profile

    def load(self, cancelled: threading.Event | None = None) -> None:
        """Load reusable resources, if this adapter supports residency."""

    def close(self) -> None:
        """Release reusable resources held by this adapter."""

    @abstractmethod
    def run(self, context: RunContext) -> RunResult:
        raise NotImplementedError


BUILTINS = {
    "command": "modelmux.adapters.command:CommandAdapter",
    "copy": "modelmux.adapters.copy:CopyAdapter",
}


def load_adapter(profile: Profile) -> Adapter:
    reference = BUILTINS.get(profile.adapter, profile.adapter)
    if ":" not in reference:
        raise ModelMuxError(
            f"Adapter must be a built-in name or module:Class reference: {reference}"
        )
    module_name, class_name = reference.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        adapter_class = getattr(module, class_name)
        adapter = adapter_class(profile)
    except (ImportError, AttributeError, TypeError) as error:
        raise ModelMuxError(f"Cannot load adapter {reference}: {error}") from error
    if not isinstance(adapter, Adapter):
        raise ModelMuxError(f"Adapter does not inherit modelmux.adapters.Adapter: {reference}")
    return adapter
