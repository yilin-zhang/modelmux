from __future__ import annotations

import argparse
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__:
    from modelmux.workers import protocol
else:  # Launched by an isolated runtime as a bare script path.
    import protocol


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run Qwen3-TTS with a local voice prompt.")
    result.add_argument("--model", required=True)
    result.add_argument("--reference-audio", required=True)
    result.add_argument("--reference-text", required=True)
    result.add_argument("--input")
    result.add_argument("--output")
    result.add_argument("--serve", action="store_true")
    result.add_argument("--language", default="chinese")
    result.add_argument("--seed", type=int, default=20260830)
    result.add_argument("--temperature", type=float, default=0.7)
    result.add_argument("--top-k", type=int, default=30)
    result.add_argument("--top-p", type=float, default=0.9)
    result.add_argument("--repetition-penalty", type=float, default=1.5)
    result.add_argument("--max-chars", type=int, default=700)
    result.add_argument("--crossfade-ms", type=int, default=80)
    return result


def hard_split(text: str, maximum: int) -> list[str]:
    pieces: list[str] = []
    remaining = text.strip()
    while len(remaining) > maximum:
        window = remaining[: maximum + 1]
        boundary = max(window.rfind(mark) for mark in "。！？；.!?;") + 1
        if boundary < maximum // 2:
            boundary = maximum
        pieces.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def sections(text: str, maximum: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    units = [piece for paragraph in paragraphs for piece in hard_split(paragraph, maximum)]
    result: list[str] = []
    current = ""
    target = max(1, int(maximum * 0.72))
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if current and (len(candidate) > maximum or len(current) >= target):
            result.append(current)
            current = unit
        else:
            current = candidate
    if current:
        result.append(current)
    return result


def crossfade_join(parts: list[object], sample_rate: int, fade_ms: int):
    import numpy as np

    arrays = [np.asarray(part, dtype=np.float32).reshape(-1) for part in parts]
    fade_samples = round(sample_rate * fade_ms / 1000)
    overlaps: list[int] = []
    total = len(arrays[0])
    for part in arrays[1:]:
        count = min(fade_samples, total, len(part))
        overlaps.append(count)
        total += len(part) - count

    joined = np.empty(total, dtype=np.float32)
    cursor = len(arrays[0])
    joined[:cursor] = arrays[0]
    for part, count in zip(arrays[1:], overlaps, strict=True):
        theta = np.linspace(0.0, np.pi / 2, count, endpoint=True, dtype=np.float32)
        if count:
            joined[cursor - count:cursor] = (
                joined[cursor - count:cursor] * np.cos(theta)
                + part[:count] * np.sin(theta)
            )
        remainder = part[count:]
        joined[cursor:cursor + len(remainder)] = remainder
        cursor += len(remainder)
    peak = float(np.max(np.abs(joined))) if joined.size else 0.0
    return joined * (0.98 / peak) if peak > 0.98 else joined


def synthesize(
    arguments: argparse.Namespace,
    *,
    model: Any | None = None,
    report: Callable[..., None] = protocol.emit_stderr,
) -> None:
    reference_audio = Path(arguments.reference_audio).expanduser().resolve()
    input_path = Path(arguments.input).expanduser().resolve()
    output_path = Path(arguments.output).expanduser().resolve()
    for label, path, kind in (
        ("Reference audio", reference_audio, "file"),
        ("Text input", input_path, "file"),
    ):
        exists = path.is_dir() if kind == "directory" else path.is_file()
        if not exists:
            raise SystemExit(f"{label} is missing: {path}")

    text = input_path.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit("Text input is empty")
    chunks = sections(text, arguments.max_chars)

    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    if model is None:
        report("progress", progress=2, phase="load", message="Loading Qwen3-TTS…")
        model = load(arguments.model)
    assert model is not None
    mx.random.seed(arguments.seed)
    rendered: list[object] = []
    sample_rate: int | None = None
    for index, chunk in enumerate(chunks, start=1):
        report(
            "progress",
            progress=3 + round((index - 1) / len(chunks) * 94),
            phase="synthesize",
            segment=index,
            segments=len(chunks),
            message=f"Generating section {index}/{len(chunks)}…",
        )
        generated = list(
            model.generate(
                text=chunk,
                lang_code=arguments.language,
                ref_audio=str(reference_audio),
                ref_text=arguments.reference_text,
                temperature=arguments.temperature,
                top_k=arguments.top_k,
                top_p=arguments.top_p,
                repetition_penalty=arguments.repetition_penalty,
                max_tokens=min(4096, max(512, len(chunk) * 5)),
                stream=False,
                verbose=False,
            )
        )
        if not generated:
            raise RuntimeError(f"Section {index} produced no audio")
        audio = np.concatenate(
            [np.asarray(item.audio, dtype=np.float32).reshape(-1) for item in generated]
        )
        chunk_rate = int(generated[0].sample_rate)
        if sample_rate is None:
            sample_rate = chunk_rate
        elif sample_rate != chunk_rate:
            raise RuntimeError("Generated sections have mismatched sample rates")
        rendered.append(audio)
        mx.clear_cache()

    assert sample_rate is not None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(
        output_path,
        crossfade_join(rendered, sample_rate, arguments.crossfade_ms),
        sample_rate,
        subtype="PCM_16",
    )
    report("progress", progress=100, phase="write", message="Audio ready")


def load(model: str) -> Any:
    """Load the Qwen3-TTS model from a concrete local directory."""
    model_path = Path(model).expanduser().resolve()
    if not model_path.is_dir():
        raise SystemExit(f"Model is missing: {model_path}")
    from mlx_audio.tts.utils import load_model

    return load_model(model_path)


def main() -> None:
    arguments = parser().parse_args()
    if arguments.serve:
        model = load(arguments.model)
        # A resident worker reports progress on stdout, alongside its replies; the
        # one-shot command reports on stderr, where `command.events: jsonl` reads it.
        protocol.serve(
            arguments,
            lambda request: synthesize(request, model=model, report=protocol.emit_stdout),
        )
        return
    if not arguments.input or not arguments.output:
        raise SystemExit("--input and --output are required")
    synthesize(arguments)


if __name__ == "__main__":
    main()
