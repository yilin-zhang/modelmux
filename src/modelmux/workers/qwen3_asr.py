from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run Qwen3-ASR from a local model directory.")
    result.add_argument("--model", required=True)
    result.add_argument("--input")
    result.add_argument("--output")
    result.add_argument("--serve", action="store_true")
    return result


def transcribe(arguments, *, session=None) -> None:
    model_path = Path(arguments.model).expanduser().resolve()
    input_path = Path(arguments.input).expanduser().resolve()
    output_path = Path(arguments.output).expanduser().resolve()
    if not model_path.is_dir():
        raise SystemExit(f"Model is not installed at {model_path}")
    if not input_path.is_file():
        raise SystemExit(f"Audio input does not exist: {input_path}")

    # This worker only accepts a concrete local path. These variables are a second
    # line of defence against accidental Hub access by transitive dependencies.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    from mlx_qwen3_asr import Session

    if session is None:
        session = Session(model=str(model_path))
    result = session.transcribe(str(input_path), return_chunks=False)
    text = str(result.text).strip()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")


def serve(arguments) -> None:
    model_path = Path(arguments.model).expanduser().resolve()
    if not model_path.is_dir():
        raise SystemExit(f"Model is not installed at {model_path}")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from mlx_qwen3_asr import Session

    session = Session(model=str(model_path))

    def emit(kind: str, **data: object) -> None:
        print(json.dumps({"type": kind, **data}, ensure_ascii=False), flush=True)

    emit("ready")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("type") == "shutdown":
                return
            if request.get("type") != "run":
                raise ValueError("unknown request type")
            values = vars(arguments).copy()
            values.update(request.get("parameters", {}))
            values["input"] = request["input_path"]
            values["output"] = request["output_path"]
            emit("progress", progress=5, message="Transcribing…")
            transcribe(argparse.Namespace(**values), session=session)
            emit("progress", progress=100, message="Transcript ready")
            emit("result")
        except Exception as error:
            emit("error", message=str(error))


def main() -> None:
    arguments = parser().parse_args()
    if arguments.serve:
        serve(arguments)
        return
    if not arguments.input or not arguments.output:
        raise SystemExit("--input and --output are required")
    transcribe(arguments)


if __name__ == "__main__":
    main()
