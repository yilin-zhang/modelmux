import stat
import sys
from pathlib import Path
from types import SimpleNamespace

from modelmux.cli import execute, parser
from modelmux.adapters.command import CommandAdapter
from modelmux.adapters.base import RunContext
from modelmux.events import Event, null_sink
from modelmux.runtime import run_profile

from conftest import make_profile


def test_run_command_downloads_artifact_and_prints_json(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "source.txt"
    output = tmp_path / "output.txt"
    source.write_text("模型接口", encoding="utf-8")
    class Client:
        settings = SimpleNamespace(base_url="http://127.0.0.1:8765")

        def submit(self, **_arguments):
            return {"id": "abc"}

        def wait(self, _run_id, *, events=False):
            return {
                "id": "abc", "status": "completed", "profile": "copy",
                "artifact_url": "/v1/jobs/abc/artifact", "metadata": {},
            }

        def download(self, _run_id, destination):
            destination.write_text("模型接口", encoding="utf-8")
            return destination.resolve()

    monkeypatch.setattr("modelmux.cli.ModelMuxClient", lambda _settings: Client())
    arguments = parser().parse_args(
        ["run", "copy", str(source), "--profile", "copy", "-o", str(output), "--json"]
    )
    assert execute(arguments) == 0
    assert output.read_text(encoding="utf-8") == "模型接口"
    assert '"profile": "copy"' in capsys.readouterr().out


def test_profiles_lists_builtins(capsys, monkeypatch) -> None:
    class Client:
        def json(self, _method, _path):
            return {"data": [{"id": "copy", "task": "copy"},
                             {"id": "qwen3-tts-0.6b-base-8bit", "task": "tts"}]}

    monkeypatch.setattr("modelmux.cli.ModelMuxClient", lambda _settings: Client())
    arguments = parser().parse_args(["profiles"])
    assert execute(arguments) == 0
    output = capsys.readouterr().out
    assert "copy\tcopy" in output
    assert "qwen3-tts-0.6b-base-8bit\ttts" in output


def test_managed_outputs_are_private(cache: Path, copy_profile) -> None:
    result = run_profile(
        task="copy",
        profile=copy_profile,
        input_bytes=b"private",
        output_path=None,
        parameters={},
        emit=null_sink,
    )
    assert stat.S_IMODE(result.output_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.output_path.parent.stat().st_mode) == 0o700


def test_command_adapter_does_not_inherit_secrets(tmp_path: Path, cache: Path, monkeypatch) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(os.environ.get('MODELMUX_TEST_SECRET', 'missing'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODELMUX_TEST_SECRET", "do-not-leak")
    profile = make_profile(
        "command-test",
        task="test",
        adapter="command",
        command={"argv": [sys.executable, str(worker), "{output_path}"]},
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


def test_command_adapter_streams_json_events(tmp_path: Path, cache: Path) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, pathlib, sys\n"
        "print(json.dumps({'type': 'progress', 'progress': 42}), file=sys.stderr)\n"
        "pathlib.Path(sys.argv[1]).write_text('done')\n",
        encoding="utf-8",
    )
    profile = make_profile(
        "event-test",
        task="test",
        adapter="command",
        command={
            "events": "jsonl",
            "argv": [sys.executable, str(worker), "{output_path}"],
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


def test_command_adapter_reuses_persistent_worker(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    counter = tmp_path / "starts"
    worker.write_text(
        "import json, pathlib, shutil, sys\n"
        "counter = pathlib.Path(sys.argv[1])\n"
        "counter.write_text(counter.read_text() + 'x' if counter.exists() else 'x')\n"
        "print(json.dumps({'type': 'ready'}), flush=True)\n"
        "for line in sys.stdin:\n"
        " request = json.loads(line)\n"
        " if request['type'] == 'shutdown': break\n"
        " shutil.copyfile(request['input_path'], request['output_path'])\n"
        " print(json.dumps({'type': 'result'}), flush=True)\n",
        encoding="utf-8",
    )
    profile = make_profile(
        "persistent-test",
        adapter="command",
        command={
            "worker_argv": [sys.executable, str(worker), str(counter)],
            "argv": [sys.executable, str(worker), str(counter)],
        },
    )
    adapter = CommandAdapter(profile)
    source = tmp_path / "input"
    source.write_text("value", encoding="utf-8")
    try:
        adapter.load()
        for index in range(2):
            output = tmp_path / f"output-{index}"
            adapter.run(
                RunContext("copy", profile, source, output, {}, null_sink)
            )
            assert output.read_text(encoding="utf-8") == "value"
    finally:
        adapter.close()
    assert counter.read_text(encoding="utf-8") == "x"
