"""Experimental, local-only YuE2 PyTorch/MPS music generation."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    """Generate a bounded music sample using existing local model weights."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--vae", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 lyrics file")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--style", required=True)
    parser.add_argument("--cot", choices=("off", "melody", "full"), default="off")
    parser.add_argument("--max-semantic-tokens", type=int, default=250)
    parser.add_argument("--seed", type=int, default=831001)
    args = parser.parse_args()
    if not 1 <= args.max_semantic_tokens <= 9000:
        parser.error("--max-semantic-tokens must be between 1 and 9000")
    output = args.output.expanduser().resolve()
    if output.suffix.lower() not in {".wav", ".flac"}:
        parser.error("--output must end in .wav or .flac")
    if output.exists():
        parser.error(f"Output already exists: {output}")
    for name in ("model", "vae"):
        path = getattr(args, name).expanduser().resolve()
        if not path.is_dir():
            parser.error(f"Local {name} directory is missing: {path}")
        setattr(args, name, path)
    try:
        lyrics = args.input.expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        parser.error(f"Cannot read lyrics: {exc}")
    if not lyrics:
        parser.error("Lyrics are empty")

    # These dependencies live in runtimes/yue2, not the gateway environment.
    import torch  # pyright: ignore[reportMissingImports]
    from yue2 import YuE2Pipeline  # pyright: ignore[reportMissingImports]

    if not torch.backends.mps.is_available():
        parser.error("This experimental worker requires Apple MPS")
    # Respect the default MPS allocation limit. A short semantic cap limits the
    # smoke run's length; it does not guarantee that full weights fit in RAM.
    with YuE2Pipeline.from_pretrained(
        str(args.model), vae=str(args.vae), local_files_only=True,
        device="mps", backend="torch-eager", quantization="none",
        memory_budget_gib=12, vae_core_frames=128, offload_ar=True,
    ) as pipe:
        song = pipe(
            style=args.style, lyrics=lyrics, cot=args.cot, seed=args.seed,
            semantic_sampling={"min_tokens": 0, "max_tokens": args.max_semantic_tokens},
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        song.save(str(output))
        print(f"Saved {output}; timing: {song.timing}")
        print(f"Truncated: {song.truncated}")


if __name__ == "__main__":
    main()
