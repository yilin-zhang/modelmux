from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any]


EventSink = Callable[[Event], None]


def null_sink(_event: Event) -> None:
    pass
