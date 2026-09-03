from __future__ import annotations

import base64
import fcntl
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import threading
import time
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from modelmux.config import ServerSettings, cache_home
from modelmux.errors import ModelMuxError
from modelmux.jobs import JobManager
from modelmux.runs import FINAL_STATUSES


MAX_REQUEST_BYTES = 256 * 1024 * 1024


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in record.items()
        if key not in {"artifact", "metadata", "pid"}
    }
    result["artifact_url"] = f"/v1/jobs/{record['id']}/artifact"
    return result


class ModelMuxHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, settings: ServerSettings, manager: JobManager) -> None:
        super().__init__((settings.host, settings.port), ModelMuxHandler)
        self.settings = settings
        self.manager = manager


class ModelMuxHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> ModelMuxHTTPServer:
        return cast(ModelMuxHTTPServer, self.server)

    def log_message(self, format: str, *args: object) -> None:
        print(f"modelmux: {self.address_string()} - {format % args}", file=sys.stderr)

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_artifact(self, path: Path, profile: str | None = None) -> None:
        """Send an artifact using its profile's media type, or one guessed from its name."""
        try:
            content_type = self.app.manager.profiles.get(str(profile)).media_type
        except ModelMuxError:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send_file(path, content_type)

    def _send_file(self, path: Path, content_type: str) -> None:
        """Send PATH without loading the complete artifact into memory."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.wfile.write(chunk)

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != self.app.settings.base_url:
            self._error(HTTPStatus.FORBIDDEN, "Cross-origin requests are not allowed")
            return False
        return True

    def _json(self, status: int, value: Any) -> None:
        self._send(status, _json_bytes(value), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": {"message": message}})

    def _body(self) -> bytes:
        return self.rfile.read(self._content_length())

    def _content_length(self) -> int:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ModelMuxError("Invalid Content-Length") from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ModelMuxError("Request body is too large")
        return length

    def _body_to_temporary_file(self) -> Path:
        """Stream the request body to a private temporary file."""
        remaining = self._content_length()
        temporary_root = cache_home() / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        temporary_root.chmod(0o700)
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="upload-", dir=temporary_root, delete=False
            ) as output:
                path = Path(output.name)
                os.fchmod(output.fileno(), 0o600)
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ModelMuxError("Upload ended before Content-Length")
                    output.write(chunk)
                    remaining -= len(chunk)
            return path
        except BaseException:
            if path is not None:
                path.unlink(missing_ok=True)
            raise

    def _json_body(self) -> dict[str, Any]:
        try:
            value = json.loads(self._body())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelMuxError(f"Invalid JSON request: {error}") from error
        if not isinstance(value, dict):
            raise ModelMuxError("JSON request must be an object")
        return value

    def _parts(self) -> dict[str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ModelMuxError("Expected multipart/form-data")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n" + self._body()
        )
        result: dict[str, bytes] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if name and isinstance(payload, bytes):
                result[str(name)] = payload
        return result

    def _run_ids(self) -> list[str]:
        """Read and validate the ``ids`` array of a batch request."""
        ids = self._json_body().get("ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise ModelMuxError("ids must be a list of run ids")
        return ids

    def _job_id(self, suffix: str = "") -> str | None:
        prefix = "/v1/jobs/"
        if not self.path.startswith(prefix):
            return None
        value = self.path[len(prefix):]
        if suffix:
            return value[: -len(suffix)] if value.endswith(suffix) else None
        return value if "/" not in value else None

    def do_GET(self) -> None:
        if not self._origin_allowed():
            return
        try:
            if self.path == "/health":
                self._json(HTTPStatus.OK, {"status": "ok"})
            elif self.path == "/v1/models":
                data = [
                    {"id": item.name, "object": "model", "task": item.task}
                    for item in self.app.manager.profiles.all()
                ]
                self._json(HTTPStatus.OK, {"object": "list", "data": data})
            elif self.path == "/v1/jobs":
                self._json(
                    HTTPStatus.OK,
                    [_public_record(item) for item in self.app.manager.runs.list()],
                )
            elif (run_id := self._job_id("/artifact")) is not None:
                record = self.app.manager.runs.get(run_id)
                artifact = Path(str(record["artifact"]))
                if record.get("status") != "completed" or not artifact.is_file():
                    self._error(HTTPStatus.CONFLICT, "Artifact is not ready")
                    return
                self._send_artifact(artifact, record.get("profile"))
            elif (run_id := self._job_id("/events")) is not None:
                self._events(run_id)
            elif (run_id := self._job_id()) is not None:
                self._json(
                    HTTPStatus.OK,
                    _public_record(self.app.manager.runs.get(run_id)),
                )
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
        except ModelMuxError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:
        if not self._origin_allowed():
            return
        try:
            if self.path == "/v1/jobs":
                payload = self._json_body()
                task = str(payload.get("task", ""))
                model = payload.get("model") or payload.get("profile")
                parameters = payload.get("parameters", {})
                if not task:
                    raise ModelMuxError("task is required")
                if not isinstance(parameters, dict):
                    raise ModelMuxError("parameters must be an object")
                if "input_base64" in payload:
                    try:
                        input_bytes = base64.b64decode(str(payload["input_base64"]), validate=True)
                    except ValueError as error:
                        raise ModelMuxError("input_base64 is invalid") from error
                else:
                    input_bytes = str(payload.get("input", "")).encode("utf-8")
                record = self.app.manager.submit(
                    task=task,
                    profile_name=str(model) if model else None,
                    input_bytes=input_bytes,
                    parameters=parameters,
                )
                self._json(HTTPStatus.ACCEPTED, _public_record(record))
            elif urlsplit(self.path).path == "/v1/jobs/upload":
                self._upload_job()
            elif self.path == "/v1/jobs/cancel":
                ids = self._run_ids()
                self._json(
                    HTTPStatus.OK,
                    [_public_record(item) for item in self.app.manager.cancel_many(ids)],
                )
            elif self.path == "/v1/jobs/delete":
                ids = self._run_ids()
                self.app.manager.runs.delete_many(ids)
                self._json(HTTPStatus.OK, {"deleted": ids})
            elif (run_id := self._job_id("/cancel")) is not None:
                self._json(HTTPStatus.OK, _public_record(self.app.manager.cancel(run_id)))
            elif self.path == "/v1/audio/speech":
                self._speech()
            elif self.path == "/v1/audio/transcriptions":
                self._transcription()
            elif self.path == "/shutdown":
                self._json(HTTPStatus.OK, {"status": "stopping"})
                threading.Thread(target=self.app.shutdown, daemon=True).start()
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
        except ModelMuxError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))

    def do_PATCH(self) -> None:
        if not self._origin_allowed():
            return
        try:
            run_id = self._job_id()
            if run_id is None:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
                return
            name = self._json_body().get("name")
            if not isinstance(name, str):
                raise ModelMuxError("name must be a string")
            self._json(
                HTTPStatus.OK,
                _public_record(self.app.manager.runs.rename(run_id, name)),
            )
        except ModelMuxError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))

    def do_DELETE(self) -> None:
        if not self._origin_allowed():
            return
        try:
            run_id = self._job_id()
            if run_id is None:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
                return
            self.app.manager.runs.delete(run_id)
            self._json(HTTPStatus.OK, {"deleted": [run_id]})
        except ModelMuxError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))

    def _speech(self) -> None:
        payload = self._json_body()
        text = payload.get("input")
        if not isinstance(text, str) or not text.strip():
            raise ModelMuxError("input must be non-empty text")
        model = payload.get("model")
        parameters = {
            key: value
            for key, value in payload.items()
            if key not in {"input", "model", "response_format", "stream_format"}
        }
        record = self.app.manager.submit(
            task="tts",
            profile_name=str(model) if model else None,
            input_bytes=text.encode("utf-8"),
            parameters=parameters,
        )
        completed = self.app.manager.wait(str(record["id"]))
        if completed["status"] != "completed":
            raise ModelMuxError(str(completed.get("error") or completed["status"]))
        self._send_artifact(Path(str(completed["artifact"])), completed.get("profile"))

    def _upload_job(self) -> None:
        """Stream a binary input into an asynchronous job."""
        if self.headers.get_content_type() != "application/octet-stream":
            raise ModelMuxError("Expected application/octet-stream")
        query = parse_qs(urlsplit(self.path).query)
        task = query.get("task", [""])[0]
        model = query.get("model", [None])[0]
        if not task:
            raise ModelMuxError("task is required")
        source = self._body_to_temporary_file()
        try:
            record = self.app.manager.submit_file(
                task=task,
                profile_name=model,
                source=source,
            )
        finally:
            source.unlink(missing_ok=True)
        self._json(HTTPStatus.ACCEPTED, _public_record(record))

    def _transcription(self) -> None:
        parts = self._parts()
        if "file" not in parts:
            raise ModelMuxError("file is required")
        model = parts.get("model", b"").decode("utf-8") or None
        record = self.app.manager.submit(
            task="asr",
            profile_name=model,
            input_bytes=parts["file"],
        )
        completed = self.app.manager.wait(str(record["id"]))
        if completed["status"] != "completed":
            raise ModelMuxError(str(completed.get("error") or completed["status"]))
        text = Path(str(completed["artifact"])).read_text(encoding="utf-8")
        self._json(HTTPStatus.OK, {"text": text})

    def _events(self, run_id: str) -> None:
        self.app.manager.runs.get(run_id)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        updated = None
        while True:
            record = self.app.manager.runs.get(run_id, reconcile=False)
            if record.get("updated_at") != updated:
                updated = record.get("updated_at")
                self.wfile.write(b"data: " + _json_bytes(_public_record(record)) + b"\n\n")
                self.wfile.flush()
            if record.get("status") in FINAL_STATUSES:
                return
            time.sleep(0.25)


def _state_paths() -> tuple[Path, Path, Path]:
    root = cache_home()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root / "server.lock", root / "server.pid", root / "server.log"


def serve(settings: ServerSettings) -> None:
    lock_path, pid_path, _log_path = _state_paths()
    with lock_path.open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ModelMuxError("ModelMux server is already running") from error
        pid_path.write_text(str(os.getpid()), encoding="ascii")
        pid_path.chmod(0o600)
        manager: JobManager | None = None
        server: ModelMuxHTTPServer | None = None
        try:
            manager = JobManager(settings)
            server = ModelMuxHTTPServer(settings, manager)
            server.serve_forever(poll_interval=0.2)
        finally:
            if server is not None:
                server.server_close()
            if manager is not None:
                manager.shutdown()
            pid_path.unlink(missing_ok=True)


def health(settings: ServerSettings, timeout: float = 0.5) -> bool:
    try:
        with urlopen(f"{settings.base_url}/health", timeout=timeout) as response:
            return (
                response.status == HTTPStatus.OK
                and json.loads(response.read()) == {"status": "ok"}
            )
    except (OSError, URLError, json.JSONDecodeError):
        return False


def start_server(settings: ServerSettings) -> None:
    if health(settings):
        raise ModelMuxError("ModelMux server is already running")
    _lock_path, _pid_path, log_path = _state_paths()
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "modelmux", "server", "run"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if health(settings):
            return
        if process.poll() is not None:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise ModelMuxError(f"Server failed to start: {detail.strip()}")
        time.sleep(0.1)
    process.terminate()
    raise ModelMuxError("Timed out waiting for ModelMux server")
