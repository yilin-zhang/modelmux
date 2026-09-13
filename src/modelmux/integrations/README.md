# Integration ownership

Each model integration is a self-contained directory:

```text
integrations/<name>/
  __init__.py          # no model imports
  worker.py            # model-specific input, inference, and output behavior
  profile.yaml         # optional; registers a built-in profile
  pyproject.toml       # isolated runtime dependencies
  uv.lock
  tests/               # this integration's tests
```

An integration without `profile.yaml` is not advertised by the gateway (currently
the experimental `yue2_torch` runner). Discovery reads YAML without importing
workers. Profile identifiers, CLI commands, and HTTP contracts do not depend on
the directory name. Weights remain in modelmux's cache, never this directory.

The core owns adapters, queueing, cancellation, HTTP, run persistence, and the
dependency-free JSON-lines protocol in `modelmux.workers.protocol`. Its tests live
in the repository's `tests/`. Integration tests own model parameter mapping,
preprocessing, progress translation and output behavior. They must not import
helpers or fixtures from the core test directory.

From the repository root:

```sh
uv run pytest                                          # all CPU-only tests
uv run pytest tests                                    # core only
uv run pytest src/modelmux/integrations/yue2_mlx/tests  # one integration
uv sync --project src/modelmux/integrations/yue2_mlx --locked
```

Pytest uses importlib mode so each integration may use names such as
`tests/test_worker.py`. Normal tests do not load weights or require model runtime
dependencies. Opt-in model tests live beside the owning integration; consult its
README before running them. Runtime environments are local `.venv` directories,
ignored by Git; remove that integration's `.venv` to uninstall its dependencies.

The wheel includes integration profiles and workers, but excludes integration
tests and virtual environments. This keeps installed and editable profile
discovery identical without requiring a checkout-relative path.
