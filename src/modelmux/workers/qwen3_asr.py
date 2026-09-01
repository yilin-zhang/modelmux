from __future__ import annotations

import argparse
import os
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run Qwen3-ASR from a local model directory.")
    result.add_argument("--model", required=True)
    result.add_argument("--input", required=True)
    result.add_argument("--output", required=True)
    return result


def main() -> None:
    arguments = parser().parse_args()
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

    session = Session(model=str(model_path))
    result = session.transcribe(str(input_path), return_chunks=False)
    text = str(result.text).strip()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
