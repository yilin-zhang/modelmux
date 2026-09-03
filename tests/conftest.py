from pathlib import Path

import pytest

from modelmux.config import Profile


def make_profile(name: str, **data) -> Profile:
    """Build a Profile literal for tests, with the common fields filled in."""
    return Profile(
        name,
        "test",
        {
            "task": "copy",
            "adapter": "copy",
            "defaults": {},
            "input": {"extension": ".txt"},
            "output": {"extension": ".txt"},
            **data,
        },
    )


@pytest.fixture
def copy_profile() -> Profile:
    return make_profile("copy-test")


@pytest.fixture
def cache(tmp_path: Path, monkeypatch) -> Path:
    """Point ModelMux's cache at a temporary directory."""
    root = tmp_path / "cache"
    monkeypatch.setenv("MODELMUX_CACHE_HOME", str(root))
    return root
