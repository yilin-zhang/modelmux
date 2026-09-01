from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def emit(kind: str, **data: object) -> None:
    print(json.dumps({"type": kind, **data}, ensure_ascii=False), file=sys.stderr, flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run Qwen3-TTS with a local voice prompt.")
    result.add_argument("--model", required=True)
    result.add_argument("--reference-audio", required=True)
    result.add_argument("--reference-text", required=True)
    result.add_argument("--input", required=True)
    result.add_argument("--output", required=True)
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

    joined = np.asarray(parts[0], dtype=np.float32).reshape(-1)
    fade_samples = round(sample_rate * fade_ms / 1000)
    for raw_part in parts[1:]:
        part = np.asarray(raw_part, dtype=np.float32).reshape(-1)
        count = min(fade_samples, len(joined), len(part))
        if count == 0:
            joined = np.concatenate([joined, part])
            continue
        theta = np.linspace(0.0, np.pi / 2, count, endpoint=True, dtype=np.float32)
        overlap = joined[-count:] * np.cos(theta) + part[:count] * np.sin(theta)
        joined = np.concatenate([joined[:-count], overlap, part[count:]])
    peak = float(np.max(np.abs(joined))) if joined.size else 0.0
    return joined * (0.98 / peak) if peak > 0.98 else joined


def main() -> None:
    arguments = parser().parse_args()
    model_path = Path(arguments.model).expanduser().resolve()
    reference_audio = Path(arguments.reference_audio).expanduser().resolve()
    input_path = Path(arguments.input).expanduser().resolve()
    output_path = Path(arguments.output).expanduser().resolve()
    for label, path, kind in (
        ("Model", model_path, "directory"),
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
    from mlx_audio.tts.utils import load_model

    emit("progress", progress=2, phase="load", message="Loading Qwen3-TTS…")
    model = load_model(str(model_path))
    mx.random.seed(arguments.seed)
    rendered: list[object] = []
    sample_rate: int | None = None
    for index, chunk in enumerate(chunks, start=1):
        emit(
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
    emit("progress", progress=100, phase="write", message="Audio ready")


if __name__ == "__main__":
    main()
