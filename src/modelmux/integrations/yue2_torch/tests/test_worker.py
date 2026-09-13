"""Validate the offline experimental entry point without installing Torch."""

import sys

import pytest

from modelmux.integrations.yue2_torch.worker import main


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--max-semantic-tokens", "0"], "must be between"),
        (["--output", "sample.mp3"], "must end in"),
        ([], "Local model directory is missing"),
    ],
)
def test_invalid_arguments_fail_before_loading_weights(monkeypatch, tmp_path, capsys, extra, message):
    monkeypatch.setattr(sys, "argv", [
        "yue2", "--model", str(tmp_path / "missing"),
        "--vae", str(tmp_path / "vae"), "--input", str(tmp_path / "lyrics.txt"),
        "--output", str(tmp_path / "sample.flac"), "--style", "acoustic", *extra,
    ])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
