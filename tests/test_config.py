from pathlib import Path

from modelmux.config import ProfileStore, apply_overrides, deep_merge, server_settings


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
