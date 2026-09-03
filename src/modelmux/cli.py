from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from modelmux import __version__
from modelmux.client import ModelMuxClient
from modelmux.config import ProfileStore, apply_overrides, cache_home, server_settings
from modelmux.errors import ModelMuxError
from modelmux.server import health, serve, start_server


TASK_ALIASES = ("tts", "asr", "ocr", "chat", "image", "embed")


def add_run_arguments(parser: argparse.ArgumentParser, *, include_task: bool) -> None:
    if include_task:
        parser.add_argument("task", help="Capability to run, such as tts, asr, chat, or image")
    parser.add_argument("input", nargs="?", default="-", help="Input file, or - for stdin")
    parser.add_argument("-p", "--profile", help="Profile/model name")
    parser.add_argument("-o", "--output", type=Path, help="Download the artifact here")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON")
    parser.add_argument("--json-events", action="store_true", help="Print progress while waiting")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="modelmux", description="Use models through a local gateway.")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    add_run_arguments(commands.add_parser("run", help="Run any task"), include_task=True)
    for task in TASK_ALIASES:
        child = commands.add_parser(task, help=f"Run a {task} profile")
        add_run_arguments(child, include_task=False)
        child.set_defaults(task=task)
    commands.add_parser("profiles", help="List models exposed by the server")
    inspect = commands.add_parser("inspect", help="Print a locally resolved profile")
    inspect.add_argument("profile")

    runs = commands.add_parser("runs", help="Manage persistent jobs")
    run_commands = runs.add_subparsers(dest="runs_command", required=True)
    run_list = run_commands.add_parser("list")
    run_list.add_argument("--json", action="store_true")
    run_show = run_commands.add_parser("show")
    run_show.add_argument("id")
    run_show.add_argument("--json", action="store_true")
    run_rename = run_commands.add_parser("rename")
    run_rename.add_argument("id")
    run_rename.add_argument("name")
    run_rename.add_argument("--json", action="store_true")
    for name in ("delete", "cancel"):
        child = run_commands.add_parser(name)
        child.add_argument("ids", nargs="+")
        child.add_argument("--json", action="store_true")

    server = commands.add_parser("server", help="Manage the local gateway")
    server_commands = server.add_subparsers(dest="server_command", required=True)
    server_commands.add_parser("start")
    server_commands.add_parser("status")
    server_commands.add_parser("stop")
    server_commands.add_parser("run", help=argparse.SUPPRESS)
    return root


def read_input(name: str) -> bytes:
    if name == "-":
        return sys.stdin.buffer.read()
    path = Path(name).expanduser()
    try:
        return path.read_bytes()
    except OSError as error:
        raise ModelMuxError(f"Cannot read input {path}: {error}") from error


def _print_runs(value: Any, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False))
        return
    records = value if isinstance(value, list) else [value]
    for record in records:
        if isinstance(record, dict):
            print("\t".join(str(record.get(key, ""))
                            for key in ("id", "name", "task", "profile", "status", "progress")))


def _run(arguments: argparse.Namespace, client: ModelMuxClient) -> int:
    parameters = apply_overrides({}, arguments.set)
    record = client.submit(
        task=arguments.task,
        model=arguments.profile,
        input_bytes=read_input(arguments.input),
        parameters=parameters,
    )
    completed = client.wait(str(record["id"]), events=arguments.json_events)
    if completed.get("status") != "completed":
        raise ModelMuxError(str(completed.get("error") or completed.get("status")))
    output: str
    if arguments.output:
        output = str(client.download(str(record["id"]), arguments.output))
    else:
        output = f"{client.settings.base_url}{completed['artifact_url']}"
    payload = {
        "id": record["id"],
        "task": arguments.task,
        "profile": completed["profile"],
        "output": output,
        "metadata": completed.get("metadata", {}),
    }
    print(json.dumps(payload, ensure_ascii=False) if arguments.json else output)
    return 0


def _runs(arguments: argparse.Namespace, client: ModelMuxClient) -> int:
    command = arguments.runs_command
    if command == "list":
        value = client.json("GET", "/v1/jobs")
    elif command == "show":
        value = client.json("GET", f"/v1/jobs/{arguments.id}")
    elif command == "rename":
        value = client.json("PATCH", f"/v1/jobs/{arguments.id}", {"name": arguments.name})
    elif command == "delete":
        value = client.json("POST", "/v1/jobs/delete", {"ids": arguments.ids})
    elif command == "cancel":
        value = client.json("POST", "/v1/jobs/cancel", {"ids": arguments.ids})
    else:
        raise ModelMuxError(f"Unknown runs command: {command}")
    _print_runs(value, json_output=arguments.json)
    return 0


def _server(arguments: argparse.Namespace) -> int:
    settings = server_settings()
    if arguments.server_command == "run":
        serve(settings)
        return 0
    if arguments.server_command == "start":
        start_server(settings)
        print(f"ModelMux server started at {settings.base_url}")
        return 0
    if arguments.server_command == "status":
        print("running" if health(settings) else "stopped")
        return 0
    if arguments.server_command == "stop":
        if not health(settings):
            raise ModelMuxError("ModelMux server is not running")
        ModelMuxClient(settings).json("POST", "/shutdown", {})
        pid_path = cache_home() / "server.pid"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and (health(settings) or pid_path.exists()):
            time.sleep(0.1)
        if health(settings) or pid_path.exists():
            raise ModelMuxError("Timed out waiting for ModelMux server to stop")
        print("ModelMux server stopped")
        return 0
    raise ModelMuxError(f"Unknown server command: {arguments.server_command}")


def execute(arguments: argparse.Namespace) -> int:
    if arguments.command == "server":
        return _server(arguments)
    if arguments.command == "inspect":
        print(yaml_dump(ProfileStore().get(arguments.profile).data), end="")
        return 0
    client = ModelMuxClient(server_settings())
    if arguments.command == "runs":
        return _runs(arguments, client)
    if arguments.command == "profiles":
        response = client.json("GET", "/v1/models")
        for profile in response.get("data", []):
            print(f"{profile['id']}\t{profile['task']}")
        return 0
    return _run(arguments, client)


def yaml_dump(value: dict[str, Any]) -> str:
    import yaml

    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def main() -> None:
    try:
        status = execute(parser().parse_args())
    except ModelMuxError as error:
        print(f"modelmux: {error}", file=sys.stderr)
        status = 2
    except KeyboardInterrupt:
        print("modelmux: cancelled", file=sys.stderr)
        status = 130
    raise SystemExit(status)
