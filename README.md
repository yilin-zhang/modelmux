# ModelMux

ModelMux is a local HTTP gateway for AI models. Emacs, the CLI, and other clients use
one stable API; server-side profiles and adapters select the actual local or remote
backend.

## Quick start

```sh
uv run modelmux server start
uv run modelmux profiles
printf '这是一个测试。' | uv run modelmux tts -o /tmp/test.wav
afplay /tmp/test.wav
uv run modelmux server stop
```

The CLI does not start the server implicitly. Use `modelmux server status` to check it.

The default TTS profile is Qwen3-TTS Base 0.6B 8-bit. It applies the same reference
audio and transcript to every coarse paragraph group, then joins groups with a short
crossfade. Repository defaults use ModelMux's standard cache paths; machine-specific
model, runtime, and voice-prompt paths belong in the user configuration.

The built-in `qwen3-asr-0.6b` profile expects its model in ModelMux's standard
cache location and forces offline mode:

```sh
uv run modelmux asr recording.wav --profile qwen3-asr-0.6b -o transcript.txt
```

Machine-specific paths belong in `~/.config/modelmux/config.yaml`, not in the
repository profile. For example, an existing model installed by another app can
be reused without moving or copying it:

```yaml
profiles:
  qwen3-asr-0.6b:
    defaults:
      model_path: /path/to/existing/model
      runtime_python: /path/to/python-with-mlx-qwen3-asr
```

The built-in Qwen workers have isolated environments owned by this repository.
Create them once, then point the corresponding user profile at the resulting
Python executable:

```sh
uv sync --project runtimes/qwen3-tts
uv sync --project runtimes/qwen3-asr
```

The executables are `runtimes/qwen3-tts/.venv/bin/python` and
`runtimes/qwen3-asr/.venv/bin/python`. Keeping model-specific packages outside
ModelMux's core environment avoids dependency conflicts between backends.

Run the dependency-free integration profile:

```sh
printf 'hello' | uv run modelmux run copy --profile copy
```

Every submitted job creates a persistent run record. Managed outputs live beside their
metadata under `runs/<uuid>/`; input contents and resolved parameters are not recorded.
The server remains responsive while model work runs in its worker pool.

```sh
modelmux runs list --json
modelmux runs rename RUN_ID "Article title" --json
modelmux runs cancel RUN_ID --json
modelmux runs delete RUN_ID --json
```

Deleting a managed run removes its artifact. An explicit `--output` remains
caller-owned and is not deleted with the run record. Runs are never pruned automatically.

## Profiles

Put YAML or JSON profiles in `~/.config/modelmux/profiles/`. A profile either names
a built-in adapter or imports a Python adapter using `package.module:ClassName`.

```yaml
name: my-tts
task: tts
adapter: my_package.vibevoice:VibeVoiceAdapter
model: some/model-id
defaults:
  voice: narrator
  generation:
    seed: 42
output:
  extension: .wav
capabilities:
  streaming: true
  reference_audio: true
```

CLI values override profile defaults and may be nested:

```sh
modelmux tts article.txt --profile my-tts \
  --set voice=serena --set generation.seed=7 -o article.wav
```

Optional user-wide overrides live in `~/.config/modelmux/config.yaml`:

```yaml
defaults:
  tts: my-tts
profiles:
  my-tts:
    defaults:
      voice: serena
```

Resolution order is: profile defaults, user profile override, command-line `--set`.

Server behavior is configured in the same private YAML:

```yaml
server:
  host: 127.0.0.1
  port: 8765
  concurrency: 1
  model_loading: lazy  # ephemeral | lazy | preload
  preload: []
```

`lazy` keeps a reusable model worker resident and, with the default single worker,
unloads it when switching profiles. `preload` loads the named profiles at server start.
`ephemeral` starts a fresh model command for each job.

## HTTP API

- `POST /v1/jobs` submits an asynchronous job
- `POST /v1/jobs/upload?task=…&model=…` streams a binary input into an asynchronous job
- `GET /v1/jobs` and `GET /v1/jobs/:id` return persistent state
- `GET /v1/jobs/:id/events` streams state changes as SSE
- `GET /v1/jobs/:id/artifact` downloads the result
- `POST /v1/jobs/cancel` and `POST /v1/jobs/delete` accept an `ids` array
- `POST /v1/audio/speech` is OpenAI-compatible TTS
- `POST /v1/audio/transcriptions` is OpenAI-compatible ASR
- `GET /v1/models` lists configured profiles

## Adapter contract

Subclass `modelmux.adapters.Adapter` and implement `run(context)`. The context contains
the task, resolved profile, temporary input path, requested output path, merged
parameters, an event callback, and a cancellation event. Return `RunResult` with the
output path and metadata. Optional `load()` and `close()` hooks own resident resources.

The built-in `command` adapter is useful when a model already provides a CLI. Its
`command.argv` is always executed directly, never through a shell. Profiles may also
provide `command.worker_argv` for a reusable JSON-lines worker; Qwen3 TTS and ASR do so.

## Emacs

Add `elisp/` to `load-path`, require `modelmux`, and point it at this checkout while
developing:

```elisp
(add-to-list 'load-path "/path/to/modelmux/elisp")
(require 'modelmux)
(setq modelmux-command
      '("uv" "run" "--project" "/path/to/modelmux" "modelmux"))
```

Commands:

- `M-x modelmux-server-start`, `modelmux-server-status`, and `modelmux-server-stop`
- `M-x modelmux-speak` generates speech for the active region, or the entire buffer when no region is active
- `M-x modelmux-transcribe` streams a selected audio file without blocking Emacs and generates a text artifact
- `M-x modelmux-tasks` opens the live task and artifact table
- `M-x modelmux-stop`

Speech generation does not start playback automatically. The task table uses `RET` or
`o` to open an artifact with the system default app, `O` to open its directory,
`e` to rename, and `k` to cancel. Mark rows with `m`, unmark with `u` or `U`, and
delete the marked rows (or the current row) with `D`. `g` refreshes immediately;
visible task buffers also refresh automatically. Emacs sends HTTP requests directly;
the CLI is used only for starting and stopping the detached server.

## Local files

- Configuration: `~/.config/modelmux/`
- Run metadata and managed outputs on macOS: `~/Library/Caches/modelmux/runs/<uuid>/`
- Scratch space on macOS: `~/Library/Caches/modelmux/tmp/`
- Detached server log and PID on macOS: `~/Library/Caches/modelmux/server.{log,pid}`

ModelMux has no telemetry. A configured remote adapter may make network requests;
individual adapters must document download and network behavior explicitly.

## Security model

Profiles and adapters are executable configuration and must be treated as trusted code.
ModelMux never fetches profiles remotely. The built-in command adapter executes an argv
array without a shell and passes only a small allowlist of non-secret environment
variables to child processes. Secrets must be opted into explicitly by an adapter.

Generated files in ModelMux's managed cache are user-only (`0600`) and its runtime
directories are `0700`. Files written to an explicit `--output` path keep the caller's
normal permissions. Run metadata is written atomically and does not contain input
contents or merged profile parameters. HTTP responses omit server filesystem paths,
adapter metadata, and process IDs. The server binds only to localhost and rejects
foreign browser origins. User
configuration and model weights are ignored by git.
