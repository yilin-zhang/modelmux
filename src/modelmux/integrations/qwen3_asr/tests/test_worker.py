"""ASR input/output contract, independent of GPU availability."""

from types import SimpleNamespace

import pytest

from modelmux.integrations.qwen3_asr.worker import transcribe


def test_transcribe_writes_utf8_text(tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio supplied to session")
    output = tmp_path / "nested" / "transcript.txt"

    class Session:
        def transcribe(self, path, *, return_chunks):
            assert path == str(audio)
            assert not return_chunks
            return SimpleNamespace(text="  转录结果\n")

    transcribe(SimpleNamespace(input=audio, output=output), session=Session())
    assert output.read_text(encoding="utf-8") == "转录结果"


def test_missing_audio_does_not_create_artifact(tmp_path):
    output = tmp_path / "transcript.txt"
    with pytest.raises(SystemExit):
        transcribe(SimpleNamespace(input=tmp_path / "missing.wav", output=output))
    assert not output.exists()
