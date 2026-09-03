from pathlib import Path

import pytest

from modelmux.config import (
    ProfileStore,
    apply_overrides,
    deep_merge,
    parse_override,
    server_settings,
)
from modelmux.errors import ModelMuxError


def test_deep_merge_preserves_nested_defaults() -> None:
    result = deep_merge(
        {"defaults": {"voice": "a", "generation": {"seed": 1, "steps": 10}}},
        {"defaults": {"generation": {"seed": 2}}},
    )
    assert result == {
        "defaults": {"voice": "a", "generation": {"seed": 2, "steps": 10}}
    }


def test_command_line_overrides_support_nested_values() -> None:
    result = apply_overrides(
        {"voice": "a", "generation": {"seed": 1}},
        ["voice=b", "generation.seed=42", "streaming=true"],
    )
    assert result == {"voice": "b", "generation": {"seed": 42}, "streaming": True}


def test_user_profile_replaces_builtin(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "copy.yaml").write_text(
        "name: copy\ntask: custom\nadapter: copy\ndefaults: {}\n", encoding="utf-8"
    )
    assert ProfileStore(tmp_path).get("copy").task == "custom"


def test_server_settings_are_loaded_from_user_yaml(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "server:\n  port: 9999\n  concurrency: 2\n"
        "  model_loading: preload\n  preload: [copy]\n",
        encoding="utf-8",
    )
    settings = server_settings(tmp_path)
    assert settings.port == 9999
    assert settings.concurrency == 2
    assert settings.model_loading == "preload"
    assert settings.preload == ("copy",)


@pytest.mark.parametrize(
    ("server_yaml", "message"),
    [
        ("host: 0.0.0.0", "only listen on localhost"),
        ("port: not-a-number", "must be integers"),
        ("port: 70000", "between 1 and 65535"),
        ("concurrency: 0", "at least 1"),
        ("model_loading: eager", "ephemeral, lazy, or preload"),
        ("preload: copy", "list of profile names"),
    ],
)
def test_server_settings_reject_invalid_values(
    tmp_path: Path, server_yaml: str, message: str
) -> None:
    (tmp_path / "config.yaml").write_text(f"server:\n  {server_yaml}\n", encoding="utf-8")
    with pytest.raises(ModelMuxError, match=message):
        server_settings(tmp_path)


@pytest.mark.parametrize(
    ("profile_yaml", "message"),
    [
        ("name: broken\nadapter: copy\n", "missing 'task'"),
        ("name: broken\ntask: tts\n", "missing 'adapter'"),
        ("name: broken\ntask: tts\nadapter: copy\ndefaults: no\n", "must be a mapping"),
    ],
)
def test_invalid_profiles_are_rejected(tmp_path: Path, profile_yaml: str, message: str) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "broken.yaml").write_text(profile_yaml, encoding="utf-8")
    with pytest.raises(ModelMuxError, match=message):
        ProfileStore(tmp_path).get("broken")


def test_overrides_reject_malformed_input() -> None:
    with pytest.raises(ModelMuxError, match="KEY=VALUE"):
        parse_override("novalue")
    with pytest.raises(ModelMuxError, match="cannot be empty"):
        parse_override("=value")
    with pytest.raises(ModelMuxError, match="Cannot set nested value"):
        apply_overrides({"voice": "a"}, ["voice.pitch=2"])


def test_profile_media_type_prefers_the_declared_value() -> None:
    store = ProfileStore()
    assert store.get("qwen3-tts-0.6b-base-8bit").media_type == "audio/wav"
    assert store.get("copy").media_type == "text/plain"
