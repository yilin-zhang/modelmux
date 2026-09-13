"""Offline YuE2 MLX music worker using a reviewed, pinned upstream implementation."""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import resource
import sys
import time
from dataclasses import replace

if __package__:
    from modelmux.workers import protocol
else:
    import protocol


REVISION = "fe0a9050fd658257b486b880422d8872ee1f81e3"
SOURCE_HASHES = {
    "generate.py": "5de06be8d3db45dc72c7cd780ed50e6bbac9453b272992b32a1c81d8e042fc0d",
    "yue2_model.py": "ef5230b3dea7f5b4823c1c1de159228c7199266b30ea68ca79ced4f0c7d0190e",
    "yue2_vae.py": "f45e397485be682649b36257e9c76b9184fa460f0abedf05ff3bb21c66ab216b",
}


def parser() -> argparse.ArgumentParser:
    """Build the standalone and persistent worker argument parser."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--model", required=True, help="Local variant directory, e.g. 8bit")
    result.add_argument("--source", required=True, help="Local pinned upstream Python directory")
    result.add_argument("--serve", action="store_true")
    result.add_argument("--input")
    result.add_argument("--output")
    result.add_argument("--style", default="Mandarin, acoustic pop, warm clear vocals")
    result.add_argument("--cot", choices=("off", "melody", "full"), default="full")
    result.add_argument("--seed", type=int, default=831001)
    result.add_argument("--max-semantic-tokens", type=int, default=750)
    result.add_argument("--steps", type=int, default=32)
    result.add_argument("--cfg-scale", type=lambda value: None if value == "None" else float(value))
    result.add_argument("--vae-core-frames", type=int, default=128)
    return result


def validate_request(args) -> tuple[str, Path]:
    """Reject malformed requests before spending time or memory on inference."""
    for name, lower, upper in (("max_semantic_tokens", 1, 9000), ("steps", 1, 64),
                                ("vae_core_frames", 16, 512), ("seed", 0, 2**32 - 1)):
        value = getattr(args, name)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer between {lower} and {upper}")
    if args.cot not in ("off", "melody", "full"):
        raise ValueError("cot must be off, melody or full")
    if not isinstance(args.style, str) or not args.style.strip():
        raise ValueError("style must be nonempty text")
    if args.cfg_scale is not None and (
        type(args.cfg_scale) not in (int, float)
        or not math.isfinite(args.cfg_scale) or not 0 < args.cfg_scale <= 3
    ):
        raise ValueError("cfg_scale must be a finite number in (0, 3]")
    if not args.input or not args.output:
        raise ValueError("input and output are required")
    input_path, output_path = Path(args.input).expanduser(), Path(args.output).expanduser()
    if input_path.stat().st_size > 65536:
        raise ValueError("Lyrics input must be at most 64 KiB")
    lyrics = input_path.read_text(encoding="utf-8").strip()
    if not lyrics:
        raise ValueError("Lyrics are empty")
    if output_path.suffix.lower() != ".wav":
        raise ValueError("output must end in .wav")
    if output_path.exists():
        raise ValueError(f"Output already exists: {output_path}")
    return lyrics, output_path


def verify_source(source: Path) -> None:
    """Only execute the reviewed upstream source revision, not arbitrary cached code."""
    for name, expected in SOURCE_HASHES.items():
        path = source / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Missing or changed upstream code: {path}; install revision {REVISION}")


def load_upstream(source: Path):
    """Import the locally installed, hash-checked upstream module without Hub access."""
    verify_source(source)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    sys.path.insert(0, str(source))
    spec = importlib.util.spec_from_file_location("modelmux_yue2_upstream", source / "generate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules.
    spec.loader.exec_module(module)
    return module


class Progress:
    """Translate upstream stage logs; percentages are estimates, not elapsed-time ratios."""

    def __init__(self, emit, token_budget: int):
        self.emit, self.token_budget = emit, token_budget
        self.percent = 5
        self.truncated = False

    def __call__(self, message: str) -> None:
        if message.startswith("[semantic]"):
            self.percent = max(self.percent, 20)
            match = re.match(r"\[semantic\] (\d+) tokens,", message)
            if match:
                self.percent = max(self.percent, 20 + min(40, int(match[1]) / self.token_budget * 40))
            if "hit max_tokens" in message:
                self.truncated = True
        elif message.startswith("[nar]"):
            self.percent = max(self.percent, 60)
            match = re.search(r"step (\d+)/(\d+)", message)
            if match:
                self.percent = max(self.percent, 60 + 30 * int(match[1]) / int(match[2]))
        elif message.startswith("[vae]"):
            self.percent = max(self.percent, 92)
        self.emit("progress", progress=self.percent, message=message)


class Session:
    """Keep one model resident, with per-request controls and bounded VAE tiles."""

    def __init__(self, args):
        load_start = time.perf_counter()
        source, model = Path(args.source).expanduser().resolve(), Path(args.model).expanduser().resolve()
        for name in ("config.json", "model.safetensors", "vae.safetensors", "vae_config.json",
                     "qwen.tiktoken", "yue2_generation_config.json"):
            if not (model / name).is_file():
                raise ValueError(f"Model file is missing: {model / name}")
        upstream = self.upstream = load_upstream(source)
        if not upstream.mx.metal.is_available():
            raise ValueError("YuE2 MLX requires an Apple Silicon GPU")

        class TiledPipeline(upstream.Yue2Pipeline):
            core_frames = 128

            def decode(self, latents):
                upstream.mx.clear_cache()
                return upstream.mx.clip(self.vae.decode_tiled(latents, core=self.core_frames), -1, 1)

        upstream.mx.reset_peak_memory()
        self.pipe = TiledPipeline(model, log=lambda message: print(message, file=sys.stderr))
        self.load_peak_gib = upstream.mx.get_peak_memory() / 2**30
        self.load_seconds = time.perf_counter() - load_start
        upstream.mx.clear_cache()

    def run(self, args, emit) -> None:
        """Generate a WAV and diagnostics sidecar; release transient arrays after each run."""
        lyrics, output = validate_request(args)
        upstream, pipe = self.upstream, self.pipe
        mx = upstream.mx
        progress = Progress(emit, args.max_semantic_tokens)
        pipe.log = progress
        pipe.core_frames = args.vae_core_frames
        sampling = replace(pipe.semantic_sampling, max_tokens=args.max_semantic_tokens,
                           min_tokens=min(pipe.semantic_sampling.min_tokens, args.max_semantic_tokens))
        mx.reset_peak_memory()
        start = time.perf_counter()
        try:
            audio, info = pipe(args.style, lyrics, cot=args.cot, seed=args.seed,
                               cfg_scale=args.cfg_scale, steps=args.steps, semantic_sampling=sampling)
            mx.eval(audio)
            if audio.ndim != 2 or audio.shape[1] != 2 or audio.shape[0] == 0:
                raise ValueError("Model returned an invalid stereo waveform")
            if not mx.all(mx.isfinite(audio)).item():
                raise ValueError("Model returned non-finite audio")
            metrics = {
                "upstream_revision": REVISION,
                "generation_seconds": time.perf_counter() - start,
                "audio_seconds": audio.shape[0] / upstream.SAMPLE_RATE,
                "peak_mlx_gib": mx.get_peak_memory() / 2**30,
                "load_peak_mlx_gib": self.load_peak_gib,
                "load_seconds": self.load_seconds,
                "process_peak_rss_gib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30,
                "semantic_truncated": progress.truncated,
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            # Write the primary artifact last; failure must not look like a finished WAV.
            output.with_suffix(".metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
            if info["abc"] is not None:
                output.with_suffix(".abc").write_text(info["abc"], encoding="utf-8")
            upstream.write_wav(output, audio)
            emit("progress", progress=100, message=(
                f"Music ready: {metrics['audio_seconds']:.1f}s audio, "
                f"{metrics['generation_seconds']:.1f}s generation, "
                f"{metrics['peak_mlx_gib']:.2f} GiB MLX peak"
                + (" (length cap reached)" if progress.truncated else "")
            ))
        finally:
            audio = info = None
            pipe.log = lambda message: print(message, file=sys.stderr)
            gc.collect()
            mx.clear_cache()


def main() -> None:
    """Run once or serve the standard modelmux JSON-lines worker protocol."""
    args = parser().parse_args()
    if args.serve:
        session = Session(args)
        protocol.serve(args, lambda request: session.run(request, protocol.emit_stdout))
    else:
        validate_request(args)
        protocol.emit_stderr("progress", progress=1, message="Loading YuE2 MLX 8-bit…")
        Session(args).run(args, protocol.emit_stderr)


if __name__ == "__main__":
    main()
