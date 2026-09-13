# YuE2 MLX music generation

The `yue2-3b-mlx-8bit` profile accepts UTF-8 lyrics and produces a 48 kHz stereo
WAV. It uses the existing command adapter and worker protocol: queued jobs,
progress, cancellation, lazy/preloaded workers, and persistent artifacts all use
the normal modelmux lifecycle. No daemon, API, or scheduler specific to music is
introduced. A worker exits when cancelled, and the next request reloads it.

## Install

From the repository root:

```sh
uv sync --project src/modelmux/integrations/yue2_mlx --locked
hf download ahmadw/YuE2-3B-MLX \
  generate.py yue2_model.py yue2_vae.py README.md \
  8bit/config.json 8bit/model.safetensors 8bit/vae.safetensors \
  8bit/vae_config.json 8bit/qwen.tiktoken 8bit/yue2_generation_config.json \
  --revision fe0a9050fd658257b486b880422d8872ee1f81e3 \
  --local-dir "$HOME/Library/Caches/modelmux/models/yue2-3b-mlx"
```

Only one weight variant is downloaded (~4.2 GB); no conversion or Torch is needed.
Add this entry under `profiles` in your **private**
`~/.config/modelmux/config.yaml`, preserving other entries:

```yaml
profiles:
  yue2-3b-mlx-8bit:
    defaults:
      runtime_python: /absolute/path/to/modelmux/src/modelmux/integrations/yue2_mlx/.venv/bin/python
```

Bundled YAML contains portable defaults, not a developer's checkout path. To use
another cache location, override both `source_path` and `model_path` privately.
Uninstall dependencies by removing `src/modelmux/integrations/yue2_mlx/.venv`; to reclaim weights
too, remove only `~/Library/Caches/modelmux/models/yue2-3b-mlx` and its private
profile override. Generated runs remain until manually deleted through modelmux.

## Run

```sh
modelmux server start  # only if it is not already running
modelmux music src/modelmux/integrations/yue2_mlx/lyrics-example.txt \
  --profile yue2-3b-mlx-8bit \
  --set 'style=Mandarin, acoustic pop, warm clear vocals' \
  --json-events -o song.wav
```

In Emacs, `M-x modelmux-music` uses the active region or current buffer as lyrics,
prompts for style, and opens the task table. `o` opens the finished audio, `O` its
download directory, and `C-c C-k` cancels a task. Audio is not automatically played.
`modelmux-music-profile` and `modelmux-music-style` are customizable.

Defaults and CLI overrides:

| Parameter | Default | Meaning |
|---|---|---|
| `cot` | `full` | Melody/chord planning; also `melody` or `off` |
| `max_semantic_tokens` | `750` | Maximum ~30 seconds, at 25 tokens/second |
| `steps` | `32` | NAR midpoint flow-matching steps |
| `seed` | `831001` | Reproducible within this MLX implementation |
| `cfg_scale` | `null` | Upstream mode-specific guidance default |
| `vae_core_frames` | `128` | Decode tile size; halo remains upstream's 16 frames |

The short default is deliberate for 16 GB Macs. It can **cut off the song**, not
make the model write a naturally completed 30-second song. For longer generation,
increase the cap explicitly, e.g. `--set max_semantic_tokens=4500` for at most
three minutes. Long songs are not yet memory-tested here. Full/melody planning
adds its own tokens and runtime independently of the audio cap.

Progress messages report actual stages and sampled tokens/ODE steps. Percentages
are stage-weighted estimates against the configured token cap, not time remaining.
The worker saves `artifact.abc` (when planning is enabled) and
`artifact.metrics.json` beside the managed WAV in the server's run directory.
Only the primary WAV is downloaded by the artifact API/Emacs command. Metrics
record truncation, generation duration, load duration/peak, MLX peak allocation,
and process-lifetime peak RSS. On macOS RSS is **not** a reliable total including
Metal allocations; do not add it to MLX peak or treat it as whole-machine usage.

## Verification and limits

Normal integration tests require no weights or MLX installation:

```sh
uv run pytest src/modelmux/integrations/yue2_mlx/tests
```

After installing the local runtime and weights, explicitly enable the four-second
model smoke test (missing models fail rather than downloading):

```sh
MODELMUX_RUN_MODEL_TESTS=1 uv run pytest src/modelmux/integrations/yue2_mlx/tests -m model
```

CLI `--set` values use YAML parsing. Quote string values such as `off`:
`--set 'cot="off"'` (unquoted `off` is interpreted as a boolean).

M4 MacBook Pro, 16 GB unified memory, MLX 0.32.2, 8-bit, 32 NAR steps,
128-frame VAE tiles, example lyrics:

| Run | Generated audio | Generation time (excluding model load) | MLX peak |
|---|---:|---:|---:|
| Standalone, `cot=off`, cap 250 | 10.0 s | 31.7 s | 6.72 GiB |
| Through HTTP gateway, `cot=full`, cap 750 | 30.0 s | 86.6 s | 6.55 GiB |

Both reached their audio caps. These are short-run measurements, not a full-song
memory guarantee or an audio-quality evaluation. The measured M4 generation is
slower than playback. macOS and other applications need additional memory. MLX
allocator safeguards are left enabled, and transient caches are cleared between
runs and before VAE decoding. Model weights remain resident in lazy mode.
Real gateway cancellation and subsequent worker reload/generation also passed;
the recovery load took 1.48 s with 4.22 GiB peak MLX allocation.

## Upstream and privacy

- Source/weights: [ahmadw/YuE2-3B-MLX](https://huggingface.co/ahmadw/YuE2-3B-MLX),
  revision `fe0a9050fd658257b486b880422d8872ee1f81e3`.
- The worker checks SHA-256 of all three inference source files before import;
  updating upstream requires a new review and explicit hash changes.
- Inference only reads local files. No automatic downloads, external requests,
  or telemetry were found in the pinned inference code; HF offline/telemetry
  settings are also set defensively. This is not an OS-level network sandbox.
- Model weights inherit **CC BY-NC 4.0 (noncommercial)**. The upstream repository
  documents its VAE-related MIT third-party attribution. Upstream source is kept
  separate from modelmux's MIT-licensed code; it is not vendored or relicensed.
