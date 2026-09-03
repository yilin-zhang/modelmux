from __future__ import annotations

import argparse
import os
from pathlib import Path

if __package__:
    from modelmux.workers import protocol
else:  # Launched by an isolated runtime as a bare script path.
    import protocol


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run Qwen3-ASR from a local model directory.")
    result.add_argument("--model", required=True)
    result.add_argument("--input")
    result.add_argument("--output")
    result.add_argument("--serve", action="store_true")
    return result


def load_session(model: str):
    """Open a Qwen3-ASR session against a concrete local model directory."""
    model_path = Path(model).expanduser().resolve()
    if not model_path.is_dir():
        raise SystemExit(f"Model is not installed at {model_path}")

    # This worker only accepts a concrete local path. These variables are a second
    # line of defence against accidental Hub access by transitive dependencies.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    from mlx_qwen3_asr import Session

    return Session(model=str(model_path))


def transcribe(arguments, *, session=None) -> None:
    input_path = Path(arguments.input).expanduser().resolve()
    output_path = Path(arguments.output).expanduser().resolve()
    if not input_path.is_file():
        raise SystemExit(f"Audio input does not exist: {input_path}")
    if session is None:
        session = load_session(arguments.model)
    result = session.transcribe(str(input_path), return_chunks=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(str(result.text).strip(), encoding="utf-8")


def main() -> None:
    arguments = parser().parse_args()
    if arguments.serve:
        session = load_session(arguments.model)

        def handle(request) -> None:
            protocol.emit_stdout("progress", progress=5, message="Transcribing…")
            transcribe(request, session=session)
            protocol.emit_stdout("progress", progress=100, message="Transcript ready")

        protocol.serve(arguments, handle)
        return
    if not arguments.input or not arguments.output:
        raise SystemExit("--input and --output are required")
    transcribe(arguments)


if __name__ == "__main__":
    main()
