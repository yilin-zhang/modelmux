from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


EventSink = Callable[[Event], None]


def stderr_sink(json_events: bool) -> EventSink:
    def emit(event: Event) -> None:
        if json_events:
            print(json.dumps(event.as_dict(), ensure_ascii=False), file=sys.stderr, flush=True)
            return
        message = event.data.get("message")
        if message:
            print(message, file=sys.stderr, flush=True)

    return emit


def null_sink(_event: Event) -> None:
    pass
