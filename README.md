# ModelMux

ModelMux is a small local interface for running AI models from the command line,
Emacs, or any other frontend. Profiles declare configuration; Python adapters own
the model-specific code.

## Quick start

```sh
uv run modelmux profiles
printf '这是一个测试。' | uv run modelmux tts --profile macos-say -o /tmp/test.aiff
afplay /tmp/test.aiff
```

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

Run the dependency-free integration profile:

```sh
printf 'hello' | uv run modelmux run copy --profile copy
```

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

## Adapter contract

Subclass `modelmux.adapters.Adapter` and implement `run(context)`. The context contains
the task, resolved profile, temporary input path, requested output path, merged
parameters, and an event callback. Return `RunResult` with the output path and metadata.

The built-in `command` adapter is useful when a model already provides a CLI. Its
`command.argv` is always executed directly, never through a shell.

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

- `M-x modelmux-tts-region`
- `M-x modelmux-tts-buffer`
- `M-x modelmux-stop`

## Local files

- Configuration: `~/.config/modelmux/`
- Generated outputs and scratch space on macOS: `~/Library/Caches/modelmux/`

ModelMux has no telemetry and performs no network requests itself. Individual model
adapters and their dependencies may download models; each adapter should document that
behavior explicitly.

## Security model

Profiles and adapters are executable configuration and must be treated as trusted code.
ModelMux never fetches profiles remotely. The built-in command adapter executes an argv
array without a shell and passes only a small allowlist of non-secret environment
variables to child processes. Secrets must be opted into explicitly by an adapter.

Generated files in ModelMux's managed cache are user-only (`0600`) and its runtime
directories are `0700`. Files written to an explicit `--output` path keep the caller's
normal permissions. User configuration and model weights are ignored by git.
