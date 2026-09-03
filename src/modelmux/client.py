from __future__ import annotations

import json
import mimetypes
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from modelmux.config import ServerSettings
from modelmux.errors import ModelMuxError


class ModelMuxClient:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: float | None = 30,
    ) -> tuple[bytes, str]:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        request = Request(
            f"{self.settings.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers.get_content_type()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(detail)["error"]["message"]
            except (json.JSONDecodeError, KeyError, TypeError):
                message = detail or str(error)
            raise ModelMuxError(str(message)) from error
        except (OSError, URLError) as error:
            raise ModelMuxError(
                "ModelMux server is not running; start it with `modelmux server start`"
            ) from error

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
        import base64

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
            if value.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                return value
            time.sleep(0.25)

    def download(self, run_id: str, destination: Path) -> Path:
        body, _content_type = self.request("GET", f"/v1/jobs/{run_id}/artifact", timeout=None)
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
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
