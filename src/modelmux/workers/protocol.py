"""JSON-lines protocol shared by ModelMux's bundled worker scripts.

Workers are launched by a foreign interpreter as bare script paths, so this
module is imported as a plain sibling of the worker rather than through the
``modelmux`` package.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any, TextIO


def emitter(stream: TextIO) -> Callable[..., None]:
    """Return a function writing protocol messages as JSON lines to STREAM."""

    def emit(kind: str, **data: object) -> None:
        print(json.dumps({"type": kind, **data}, ensure_ascii=False), file=stream, flush=True)

    return emit


emit_stdout = emitter(sys.stdout)
emit_stderr = emitter(sys.stderr)


def request_arguments(arguments: argparse.Namespace, request: dict[str, Any]) -> argparse.Namespace:
    """Overlay a run REQUEST's paths and parameters onto the worker defaults."""
    values = vars(arguments).copy()
    values.update(request.get("parameters", {}))
    values["input"] = request["input_path"]
    values["output"] = request["output_path"]
    return argparse.Namespace(**values)


def serve(arguments: argparse.Namespace, handle: Callable[[argparse.Namespace], None]) -> None:
    """Answer run requests on stdin until the gateway asks the worker to stop."""
    emit_stdout("ready")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("type") == "shutdown":
                return
            if request.get("type") != "run":
                raise ValueError("unknown request type")
            handle(request_arguments(arguments, request))
            emit_stdout("result")
        except Exception as error:
            emit_stdout("error", message=str(error))
