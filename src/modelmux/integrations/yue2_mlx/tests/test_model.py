"""Explicitly opt in to real local inference; never download weights from tests."""

import json
import os
from pathlib import Path
import wave

import pytest

from modelmux.adapters.base import RunContext
from modelmux.adapters.command import CommandAdapter
from modelmux.config import ProfileStore


@pytest.mark.model
@pytest.mark.skipif(os.environ.get("MODELMUX_RUN_MODEL_TESTS") != "1",
                    reason="Set MODELMUX_RUN_MODEL_TESTS=1 to run local model inference")
def test_local_music_generation(tmp_path):
    integration = Path(__file__).resolve().parents[1]
    profile = ProfileStore().get("yue2-3b-mlx-8bit")
    parameters = {**profile.defaults, "runtime_python": str(integration / ".venv/bin/python"),
                  "cot": "off", "max_semantic_tokens": 100}
    output = tmp_path / "sample.wav"
    events = []
    context = RunContext("music", profile, integration / "lyrics-example.txt", output,
                         parameters, events.append)
    result = CommandAdapter(profile).run(context)
    assert result.output_path == output
    with wave.open(str(output)) as audio:
        assert audio.getnchannels() == 2
        assert audio.getframerate() == 48000
        assert 0 < audio.getnframes() <= 4 * 48000
    metrics = json.loads(output.with_suffix(".metrics.json").read_text())
    assert metrics["generation_seconds"] > 0
    assert any(event.type == "progress" and event.data.get("progress") == 100 for event in events)
