from pathlib import Path

from modelmux.config import ProfileStore, apply_overrides, deep_merge


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
