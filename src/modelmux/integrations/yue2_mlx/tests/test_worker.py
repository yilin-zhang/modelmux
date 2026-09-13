"""Test the music worker boundary without requiring a GPU or model download."""

from pathlib import Path

import pytest

from modelmux.adapters.base import RunContext
from modelmux.adapters.command import CommandAdapter
from modelmux.config import ProfileStore
from modelmux.events import null_sink
from modelmux.integrations.yue2_mlx.worker import Progress, parser, validate_request, verify_source


@pytest.fixture
def arguments(tmp_path):
    lyrics = tmp_path / "lyrics.txt"
    lyrics.write_text("[Verse]\n晚风轻轻吹过窗边", encoding="utf-8")
    return parser().parse_args([
        "--model", str(tmp_path / "8bit"), "--source", str(tmp_path),
        "--input", str(lyrics), "--output", str(tmp_path / "song.wav"),
    ])


@pytest.mark.parametrize(("name", "value"), [
    ("max_semantic_tokens", 0), ("max_semantic_tokens", 9001), ("steps", True),
    ("steps", 65), ("vae_core_frames", 0), ("seed", -1),
    ("cot", "unknown"), ("style", " "), ("cfg_scale", float("nan")),
    ("cfg_scale", "1.2"), ("cfg_scale", 0),
])
def test_invalid_request_parameters(arguments, name, value):
    setattr(arguments, name, value)
    with pytest.raises(ValueError, match=name):
        validate_request(arguments)


def test_lyrics_and_outputs_are_validated(arguments):
    lyrics, output = validate_request(arguments)
    assert "晚风" in lyrics
    assert output.suffix == ".wav"
    output.write_bytes(b"keep")
    with pytest.raises(ValueError, match="already exists"):
        validate_request(arguments)
    assert output.read_bytes() == b"keep"
    Path(arguments.input).write_text(" " * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        validate_request(arguments)


def test_empty_lyrics_fail_before_model_load(arguments):
    Path(arguments.input).write_text(" \n")
    with pytest.raises(ValueError, match="empty"):
        validate_request(arguments)


def test_unreviewed_source_is_not_executed(tmp_path):
    (tmp_path / "generate.py").write_text("raise RuntimeError('must not execute')")
    with pytest.raises(ValueError, match="Missing or changed upstream code"):
        verify_source(tmp_path)


def test_progress_counts_generated_tokens_not_prefix_and_never_regresses():
    events = []
    progress = Progress(lambda kind, **data: events.append(data), 750)
    for log in (
        "[plan] generating ABC score", "[abc] 200 tokens, 40.0 tok/s",
        "[semantic] prefix 1000 tokens, cfg 1.0", "[semantic] 200 tokens, 20.0 tok/s",
        "[semantic] hit max_tokens", "[nar] step 32/32", "[nar] step 8/32",
        "[vae] decoding",
    ):
        progress(log)
    percentages = [event["progress"] for event in events]
    assert percentages[2] == 20
    assert percentages[3] == pytest.approx(20 + 40 * 200 / 750)
    assert percentages == sorted(percentages)
    assert percentages[-1] == 92
    assert progress.truncated


def test_music_profile_passes_same_parameters_to_one_shot_worker(tmp_path):
    profile = ProfileStore(tmp_path).get("yue2-3b-mlx-8bit")
    parameters = {**profile.defaults, "cfg_scale": 1.2, "steps": 16, "cot": "off"}
    adapter = CommandAdapter(profile)
    context = RunContext("music", profile, tmp_path / "lyrics.txt", tmp_path / "song.wav",
                         parameters, null_sink)
    parsed = parser().parse_args(adapter._argv("argv", context)[2:])
    for key in ("style", "cot", "seed", "max_semantic_tokens", "steps", "cfg_scale", "vae_core_frames"):
        assert getattr(parsed, key) == parameters[key]
    context.parameters["cfg_scale"] = None
    assert parser().parse_args(adapter._argv("argv", context)[2:]).cfg_scale is None
    assert profile.media_type == "audio/wav"
