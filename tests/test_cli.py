import stat
import sys
from pathlib import Path

from modelmux.cli import execute, parser
from modelmux.config import Profile
from modelmux.events import Event, null_sink
from modelmux.runtime import run_profile


def test_copy_adapter_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "source.txt"
    output = tmp_path / "output.txt"
    source.write_text("模型接口", encoding="utf-8")
    monkeypatch.setenv("MODELMUX_CACHE_HOME", str(tmp_path / "cache"))
    arguments = parser().parse_args(
        ["run", "copy", str(source), "--profile", "copy", "-o", str(output), "--json"]
    )
    assert execute(arguments) == 0
    assert output.read_text(encoding="utf-8") == "模型接口"
    assert '"profile": "copy"' in capsys.readouterr().out


def test_profiles_lists_builtins(capsys) -> None:
    arguments = parser().parse_args(["profiles"])
    assert execute(arguments) == 0
    output = capsys.readouterr().out
    assert "copy\tcopy\tcopy" in output
    assert "qwen3-tts-0.6b-base-8bit\ttts\tcommand" in output


def test_managed_outputs_are_private(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MODELMUX_CACHE_HOME", str(tmp_path / "cache"))
    profile = Profile(
        "copy-test",
        "test",
        {
            "task": "copy",
            "adapter": "copy",
            "defaults": {},
            "input": {"extension": ".txt"},
            "output": {"extension": ".txt"},
        },
    )
    result = run_profile(
        task="copy",
        profile=profile,
        input_bytes=b"private",
        output_path=None,
        parameters={},
        emit=null_sink,
    )
    assert stat.S_IMODE(result.output_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.output_path.parent.stat().st_mode) == 0o700


def test_command_adapter_does_not_inherit_secrets(tmp_path: Path, monkeypatch) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(os.environ.get('MODELMUX_TEST_SECRET', 'missing'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELMUX_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MODELMUX_TEST_SECRET", "do-not-leak")
    profile = Profile(
        "command-test",
        "test",
        {
            "task": "test",
            "adapter": "command",
            "defaults": {},
            "input": {"extension": ".txt"},
            "output": {"extension": ".txt"},
            "command": {"argv": [sys.executable, str(worker), "{output_path}"]},
        },
    )
    result = run_profile(
        task="test",
        profile=profile,
        input_bytes=b"",
        output_path=None,
        parameters={},
        emit=null_sink,
    )
    assert result.output_path.read_text(encoding="utf-8") == "missing"


def test_command_adapter_streams_json_events(tmp_path: Path, monkeypatch) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, pathlib, sys\n"
        "print(json.dumps({'type': 'progress', 'progress': 42}), file=sys.stderr)\n"
        "pathlib.Path(sys.argv[1]).write_text('done')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELMUX_CACHE_HOME", str(tmp_path / "cache"))
    profile = Profile(
        "event-test",
        "test",
        {
            "task": "test",
            "adapter": "command",
            "defaults": {},
            "input": {"extension": ".txt"},
            "output": {"extension": ".txt"},
            "command": {
                "events": "jsonl",
                "argv": [sys.executable, str(worker), "{output_path}"],
            },
        },
    )
    events: list[Event] = []
    run_profile(
        task="test",
        profile=profile,
        input_bytes=b"",
        output_path=None,
        parameters={},
        emit=events.append,
    )
    assert any(event.type == "progress" and event.data["progress"] == 42 for event in events)
