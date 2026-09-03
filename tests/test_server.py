from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from modelmux.client import ModelMuxClient
from modelmux.config import ProfileStore, ServerSettings
from modelmux.errors import ModelMuxError
from modelmux.jobs import JobManager
from modelmux.runs import RunStore
from modelmux.server import ModelMuxHTTPServer


@contextmanager
def running_server(
    tmp_path: Path, model_loading: str = "ephemeral", concurrency: int = 1
) -> Generator[tuple[ModelMuxClient, JobManager], None, None]:
    config = tmp_path / "config"
    profiles = config / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "fake-tts.yaml").write_text(
        "name: fake-tts\ntask: tts\nadapter: copy\n"
        "input: {extension: .txt}\noutput: {extension: .wav}\ndefaults: {}\n",
        encoding="utf-8",
    )
    (profiles / "fake-asr.yaml").write_text(
        "name: fake-asr\ntask: asr\nadapter: copy\n"
        "input: {extension: .wav}\noutput: {extension: .txt}\ndefaults: {}\n",
        encoding="utf-8",
    )
    settings = ServerSettings(
        port=0, model_loading=model_loading, concurrency=concurrency
    )
    manager = JobManager(
        settings,
        profiles=ProfileStore(config),
        runs=RunStore(tmp_path / "runs"),
    )
    server = ModelMuxHTTPServer(settings, manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ModelMuxClient(ServerSettings(port=server.server_port))
    try:
        yield client, manager
    finally:
        server.shutdown()
        server.server_close()
        manager.shutdown()
        thread.join()


def test_http_job_lifecycle(tmp_path: Path) -> None:
    with running_server(tmp_path) as (client, _manager):
        created = client.submit(
            task="copy", model="copy", input_bytes=b"hello", parameters={}
        )
        completed = client.wait(created["id"])

        assert completed["status"] == "completed"
        assert "artifact" not in completed
        assert "metadata" not in completed
        assert "pid" not in completed
        body, _ = client.request("GET", completed["artifact_url"])
        assert body == b"hello"

        renamed = client.json(
            "PATCH", f"/v1/jobs/{created['id']}", {"name": "Readable"}
        )
        assert renamed["name"] == "Readable"
        client.json("POST", "/v1/jobs/delete", {"ids": [created["id"]]})
        assert client.json("GET", "/v1/jobs") == []


def test_server_rejects_foreign_browser_origins(tmp_path: Path) -> None:
    with running_server(tmp_path) as (client, _manager):
        request = Request(
            f"{client.settings.base_url}/health",
            headers={"Origin": "https://example.com"},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 403


def test_openai_compatible_speech_and_transcription(tmp_path: Path) -> None:
    with running_server(tmp_path) as (client, _manager):
        speech, content_type = client.request(
            "POST",
            "/v1/audio/speech",
            body=json.dumps({"model": "fake-tts", "input": "你好"}).encode(),
            timeout=None,
        )
        assert speech == "你好".encode()
        assert content_type.startswith("audio/")

        source = tmp_path / "speech.wav"
        source.write_text("转录结果", encoding="utf-8")
        result = client.transcribe("fake-asr", source)
        assert result == {"text": "转录结果"}


def test_binary_upload_creates_asynchronous_job(tmp_path: Path) -> None:
    with running_server(tmp_path) as (client, manager):
        body, content_type = client.request(
            "POST",
            "/v1/jobs/upload?task=asr&model=fake-asr",
            body="转录结果".encode(),
            content_type="application/octet-stream",
        )
        created = json.loads(body)
        completed = client.wait(created["id"])

        assert content_type == "application/json"
        assert completed["status"] == "completed"
        artifact, _ = client.request("GET", completed["artifact_url"])
        assert artifact == "转录结果".encode()
        assert not manager.runs.input_path(created["id"], ".wav").exists()


def test_server_cancels_a_running_worker_without_stopping(tmp_path: Path) -> None:
    worker = tmp_path / "slow.py"
    worker.write_text(
        "import pathlib, sys, time\n"
        "time.sleep(30)\n"
        "pathlib.Path(sys.argv[1]).write_text('late')\n",
        encoding="utf-8",
    )
    with running_server(tmp_path) as (client, manager):
        profile_path = manager.profiles.root / "profiles" / "slow.json"
        profile_path.write_text(
            json.dumps({
                "name": "slow", "task": "copy", "adapter": "command",
                "defaults": {}, "input": {"extension": ".txt"},
                "output": {"extension": ".txt"},
                "command": {"argv": [sys.executable, str(worker), "{output_path}"]},
            }),
            encoding="utf-8",
        )
        created = client.submit(
            task="copy", model="slow", input_bytes=b"value", parameters={}
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            record = client.json("GET", f"/v1/jobs/{created['id']}")
            if record["status"] == "running":
                break
            time.sleep(0.02)
        client.json("POST", "/v1/jobs/cancel", {"ids": [created["id"]]})
        assert client.wait(created["id"], events=False)["status"] == "cancelled"
        assert client.json("GET", "/health") == {"status": "ok"}


@pytest.mark.parametrize("model_loading", ["lazy", "preload", "ephemeral"])
def test_every_model_loading_mode_completes_a_job(tmp_path: Path, model_loading: str) -> None:
    with running_server(tmp_path, model_loading=model_loading) as (client, _manager):
        created = client.submit(
            task="tts", model="fake-tts", input_bytes=b"hello", parameters={}
        )
        assert client.wait(created["id"])["status"] == "completed"


def test_resident_adapter_serializes_concurrent_jobs_on_one_profile(tmp_path: Path) -> None:
    with running_server(tmp_path, model_loading="lazy", concurrency=2) as (client, _manager):
        created = [
            client.submit(
                task="tts", model="fake-tts", input_bytes=f"job {index}".encode(),
                parameters={},
            )
            for index in range(4)
        ]
        for record in created:
            completed = client.wait(record["id"])
            assert completed["status"] == "completed"
            body, _ = client.request("GET", completed["artifact_url"])
            assert body.startswith(b"job ")


def test_cancelling_an_unknown_or_finished_run_is_rejected(tmp_path: Path) -> None:
    with running_server(tmp_path) as (client, _manager):
        created = client.submit(
            task="tts", model="fake-tts", input_bytes=b"hello", parameters={}
        )
        client.wait(created["id"])
        with pytest.raises(ModelMuxError, match="not active"):
            client.json("POST", "/v1/jobs/cancel", {"ids": [created["id"]]})


def test_batch_endpoints_reject_a_malformed_ids_payload(tmp_path: Path) -> None:
    with running_server(tmp_path) as (client, _manager):
        for path in ("/v1/jobs/cancel", "/v1/jobs/delete"):
            with pytest.raises(ModelMuxError, match="list of run ids"):
                client.json("POST", path, {"ids": "not-a-list"})
