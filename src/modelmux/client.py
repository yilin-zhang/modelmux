from __future__ import annotations

import base64
import json
import mimetypes
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from modelmux.config import ServerSettings
from modelmux.errors import ModelMuxError
from modelmux.runs import FINAL_STATUSES


NOT_RUNNING = "ModelMux server is not running; start it with `modelmux server start`"


class ModelMuxClient:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings

    @contextmanager
    def _open(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: float | None = 30,
        accept: str = "application/json",
    ) -> Generator[Any, None, None]:
        headers = {"Accept": accept}
        if body is not None:
            headers["Content-Type"] = content_type
        request = Request(
            f"{self.settings.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            response = urlopen(request, timeout=timeout)
        except HTTPError as error:
            raise ModelMuxError(self._error_message(error)) from error
        except (OSError, URLError) as error:
            raise ModelMuxError(NOT_RUNNING) from error
        try:
            yield response
        finally:
            response.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: float | None = 30,
    ) -> tuple[bytes, str]:
        with self._open(
            method,
            path,
            body=body,
            content_type=content_type,
            timeout=timeout,
        ) as response:
            return response.read(), response.headers.get_content_type()

    @staticmethod
    def _error_message(error: HTTPError) -> str:
        """Extract a ModelMux error message from an HTTP error response."""
        detail = error.read().decode("utf-8", errors="replace")
        try:
            return str(json.loads(detail)["error"]["message"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return detail or str(error)

    def json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = 30,
    ) -> Any:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        raw, _content_type = self.request(method, path, body=body, timeout=timeout)
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelMuxError("Server returned invalid JSON") from error

    def submit(
        self,
        *,
        task: str,
        model: str | None,
        input_bytes: bytes,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task": task,
            "model": model,
            "parameters": parameters,
            "input_base64": base64.b64encode(input_bytes).decode("ascii"),
        }
        value = self.json("POST", "/v1/jobs", payload)
        if not isinstance(value, dict):
            raise ModelMuxError("Server returned an invalid job")
        return value

    def wait(self, run_id: str, *, events: bool = False) -> dict[str, Any]:
        previous = None
        while True:
            value = self.json("GET", f"/v1/jobs/{run_id}")
            if not isinstance(value, dict):
                raise ModelMuxError("Server returned an invalid job")
            marker = (value.get("progress"), value.get("message"), value.get("status"))
            if events and marker != previous:
                print(value.get("message") or value.get("status"), file=sys.stderr)
                previous = marker
            if value.get("status") in FINAL_STATUSES:
                return value
            time.sleep(0.25)

    def download(self, run_id: str, destination: Path) -> Path:
        """Stream an artifact to DESTINATION without holding it in memory."""
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._open(
            "GET",
            f"/v1/jobs/{run_id}/artifact",
            timeout=None,
            accept="application/octet-stream",
        ) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output, 1024 * 1024)
        return destination

    def transcribe(self, model: str, source: Path) -> dict[str, Any]:
        boundary = f"modelmux-{uuid.uuid4().hex}"
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"audio\"\r\nContent-Type: {media_type}\r\n\r\n"
            ).encode() + source.read_bytes() + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        raw, _ = self.request(
            "POST",
            "/v1/audio/transcriptions",
            body=b"".join(parts),
            content_type=f"multipart/form-data; boundary={boundary}",
            timeout=None,
        )
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ModelMuxError("Server returned an invalid transcription")
        return value
