# YuE2: experimental PyTorch/MPS runner

This is a standalone smoke-generation entry point, not yet a registered modelmux
server adapter. It uses the official `yue2-infer` 0.1.5 implementation, which
already supports MPS. No architecture conversion or retraining is involved.

From the repository root:

```sh
uv sync --project runtimes/yue2 --locked
runtimes/yue2/.venv/bin/python src/modelmux/workers/yue2_torch.py \
  --model "$HOME/Library/Caches/modelmux/models/YuE2-3B" \
  --vae "$HOME/Library/Caches/modelmux/models/YuE2-Vae" \
  --input lyrics.txt --output sample.flac \
  --style 'Mandarin, acoustic pop, gentle vocals'
```

Both model directories must contain the corresponding complete official model
files. The runner uses `local_files_only=True`: it does not download weights.
The paths above are examples, not preinstalled models. Follow upstream's lyrics
format and model license (including noncommercial restrictions).

Defaults: MPS, torch-eager, unquantized BF16 AR/NAR, FP32 VAE, no melody-planning
stage (`--cot off`), 250 semantic tokens (roughly 10 seconds maximum), and
128-frame VAE chunks. The token cap can cut off the song; truncation is printed.
Use `--cot melody` or `--cot full` to enable the upstream planning stages.

`offload_ar=True` reduces GPU residency during later stages, but Apple unified
memory is still shared: moving weights to CPU does not remove their RAM cost.
Upstream's `memory_budget_gib` is not an enforced MPS allocation limit. The
runner leaves PyTorch's default MPS allocator safeguards intact. A short output
does not guarantee the full model fits in a 16 GB Mac.

Validated on an M4 / 16 GB Mac: package import and upstream BF16 grouped-query
attention on MPS. Full-weight song generation and peak memory are **not yet
validated**. No model weights were downloaded for that smoke test.

## Quantization

The official Torch package offers `none` and CUDA-oriented `fp8`; this runner
uses `none`. Community alternatives use different runtimes and cannot simply
replace this runner's weights:

- [ahmadw/YuE2-3B-MLX](https://huggingface.co/ahmadw/YuE2-3B-MLX):
  8-bit (~4.2 GB), mixed 4-bit AR / 8-bit NAR (~3.4 GB).
- [audio-cpp/Yue2-3B-GGUF](https://huggingface.co/audio-cpp/Yue2-3B-GGUF):
  Q8/Q4 variants for audio.cpp.

These sizes describe published artifacts, not peak inference RAM. Neither
community implementation has been tested here.

To uninstall the isolated dependencies, remove `runtimes/yue2/.venv`.
This does not remove any separately downloaded model weights.
