import json
from pathlib import Path

import pytest

from modelmux.cli import execute, parser
from modelmux.config import Profile
from modelmux.errors import ModelMuxError
from modelmux.events import null_sink
from modelmux.runtime import run_profile
from modelmux.runs import RunStore


def copy_profile() -> Profile:
    return Profile(
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


def test_managed_run_is_persistent_and_self_contained(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("MODELMUX_CACHE_HOME", str(cache))
    result = run_profile(
        task="copy",
        profile=copy_profile(),
        input_bytes=b"private article text",
        output_path=None,
        parameters={"secret": "not metadata"},
        emit=null_sink,
    )

    assert result.run_id is not None
    run_dir = cache / "runs" / result.run_id
    record = RunStore().get(result.run_id)
    assert record["name"] == result.run_id
    assert record["status"] == "completed"
    assert record["artifact"] == str(run_dir / "artifact.txt")
    assert Path(record["artifact"]).read_bytes() == b"private article text"
    assert "private article text" not in (run_dir / "meta.json").read_text()
    assert "not metadata" not in (run_dir / "meta.json").read_text()
    assert RunStore().list()[0]["id"] == result.run_id


def test_closed_event_consumer_does_not_fail_completed_work(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MODELMUX_CACHE_HOME", str(tmp_path / "cache"))

    def closed_consumer(_event) -> None:
        raise BrokenPipeError

    result = run_profile(
        task="copy",
        profile=copy_profile(),
        input_bytes=b"value",
        output_path=None,
        parameters={},
        emit=closed_consumer,
    )

    assert result.run_id is not None
    assert RunStore().get(result.run_id)["status"] == "completed"


def test_rename_changes_only_display_name(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record, active = store.create(
        task="tts", profile="voice", extension=".wav", output_path=None
    )
    active.close()
    artifact = record["artifact"]

    renamed = store.rename(record["id"], "Article title")

    assert renamed["name"] == "Article title"
    assert renamed["artifact"] == artifact


def test_abandoned_active_run_becomes_interrupted(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record, active = store.create(
        task="tts", profile="voice", extension=".wav", output_path=None
    )
    store.update(record["id"], status="running")
    active.close()

    reconciled = store.get(record["id"])

    assert reconciled["status"] == "interrupted"
    assert reconciled["finished_at"] is not None


def test_delete_removes_managed_artifact_and_metadata(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record, active = store.create(
        task="tts", profile="voice", extension=".wav", output_path=None
    )
    artifact = Path(record["artifact"])
    artifact.write_bytes(b"audio")
    store.finish(record["id"], "completed", message="Completed")
    active.close()

    store.delete(record["id"])

    assert not artifact.exists()
    assert not (store.root / record["id"]).exists()


def test_delete_rejects_an_active_run(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record, active = store.create(
        task="tts", profile="voice", extension=".wav", output_path=None
    )
    try:
        with pytest.raises(ModelMuxError, match="cancel it first"):
            store.delete(record["id"])
    finally:
        active.close()


def test_batch_delete_checks_every_run_before_removing_anything(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    completed, completed_lock = store.create(
        task="tts", profile="voice", extension=".wav", output_path=None
    )
    store.finish(completed["id"], "completed", message="Completed")
    completed_lock.close()
    active, active_lock = store.create(
        task="tts", profile="voice", extension=".wav", output_path=None
    )
    try:
        with pytest.raises(ModelMuxError, match="cancel it first"):
            store.delete_many([completed["id"], active["id"]])
        assert (store.root / completed["id"]).is_dir()
    finally:
        active_lock.close()


def test_delete_preserves_explicit_output(tmp_path: Path) -> None:
    output = tmp_path / "caller-owned.txt"
    store = RunStore(tmp_path / "runs")
    record, active = store.create(
        task="copy", profile="copy", extension=".txt", output_path=output
    )
    output.write_text("keep", encoding="utf-8")
    store.finish(record["id"], "completed", message="Completed")
    active.close()

    store.delete(record["id"])

    assert output.read_text(encoding="utf-8") == "keep"


def test_runs_cli_lists_renames_and_deletes(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MODELMUX_CACHE_HOME", str(tmp_path / "cache"))
    result = run_profile(
        task="copy",
        profile=copy_profile(),
        input_bytes=b"value",
        output_path=None,
        parameters={},
        emit=null_sink,
    )
    assert result.run_id is not None

    store = RunStore()

    class Client:
        def json(self, method, path, payload=None):
            if path == "/v1/jobs":
                return store.list()
            if path == "/v1/jobs/delete":
                store.delete_many(payload["ids"])
                return {"deleted": payload["ids"]}
            run_id = path.split("/")[3]
            if method == "PATCH":
                return store.rename(run_id, payload["name"])
            return store.get(run_id)

    monkeypatch.setattr("modelmux.cli.ModelMuxClient", lambda _settings: Client())

    assert execute(parser().parse_args(["runs", "list", "--json"])) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["id"] == result.run_id

    assert (
        execute(
            parser().parse_args(
                ["runs", "rename", result.run_id, "Readable name", "--json"]
            )
        )
        == 0
    )
    renamed = json.loads(capsys.readouterr().out)
    assert renamed["name"] == "Readable name"

    assert (
        execute(parser().parse_args(["runs", "delete", result.run_id, "--json"]))
        == 0
    )
    assert json.loads(capsys.readouterr().out)["deleted"] == [result.run_id]
    assert store.list() == []
