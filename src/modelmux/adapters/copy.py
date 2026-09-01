from __future__ import annotations

import shutil

from modelmux.adapters.base import Adapter, RunContext, RunResult
from modelmux.events import Event


class CopyAdapter(Adapter):
    """A dependency-free adapter used to test frontends and configuration."""

    def run(self, context: RunContext) -> RunResult:
        context.output_path.parent.mkdir(parents=True, exist_ok=True)
        context.emit(Event("progress", {"progress": 0, "message": "Copying input…"}))
        shutil.copyfile(context.input_path, context.output_path)
        context.emit(Event("progress", {"progress": 100, "message": "Done"}))
        return RunResult(context.output_path, {"adapter": "copy"})
