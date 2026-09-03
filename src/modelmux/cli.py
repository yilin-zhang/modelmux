from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from modelmux import __version__
from modelmux.config import ProfileStore, apply_overrides
from modelmux.errors import ModelMuxError
from modelmux.events import stderr_sink
from modelmux.runtime import run_profile
from modelmux.runs import RunStore


TASK_ALIASES = ("tts", "asr", "ocr", "chat", "image", "embed")


def add_run_arguments(parser: argparse.ArgumentParser, *, include_task: bool) -> None:
    if include_task:
        parser.add_argument("task", help="Capability to run, such as tts, asr, chat, or image")
    parser.add_argument("input", nargs="?", default="-", help="Input file, or - for stdin")
    parser.add_argument("-p", "--profile", help="Profile name or YAML/JSON path")
    parser.add_argument("-o", "--output", type=Path, help="Output path; defaults to the ModelMux cache")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="Override a profile default; repeatable")
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON")
    parser.add_argument("--json-events", action="store_true", help="Write JSON-lines progress events to stderr")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="modelmux", description="Run local AI models through one stable interface.")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    add_run_arguments(commands.add_parser("run", help="Run any task"), include_task=True)
    for task in TASK_ALIASES:
        child = commands.add_parser(task, help=f"Run a {task} profile")
        add_run_arguments(child, include_task=False)
        child.set_defaults(task=task)
    commands.add_parser("profiles", help="List available profiles")
    inspect = commands.add_parser("inspect", help="Print a resolved profile")
    inspect.add_argument("profile")
    runs = commands.add_parser("runs", help="Manage persistent runs")
    run_commands = runs.add_subparsers(dest="runs_command", required=True)
    run_list = run_commands.add_parser("list", help="List runs")
    run_list.add_argument("--json", action="store_true")
    run_show = run_commands.add_parser("show", help="Show one run")
    run_show.add_argument("id")
    run_show.add_argument("--json", action="store_true")
    run_rename = run_commands.add_parser("rename", help="Rename one run")
    run_rename.add_argument("id")
    run_rename.add_argument("name")
    run_rename.add_argument("--json", action="store_true")
    run_delete = run_commands.add_parser("delete", help="Delete completed runs")
    run_delete.add_argument("ids", nargs="+")
    run_delete.add_argument("--json", action="store_true")
    run_cancel = run_commands.add_parser("cancel", help="Cancel active runs")
    run_cancel.add_argument("ids", nargs="+")
    run_cancel.add_argument("--json", action="store_true")
    return root


def read_input(name: str) -> bytes:
    if name == "-":
        return sys.stdin.buffer.read()
    path = Path(name).expanduser()
    try:
        return path.read_bytes()
    except OSError as error:
        raise ModelMuxError(f"Cannot read input {path}: {error}") from error


def _run(arguments: argparse.Namespace, store: ProfileStore) -> int:
    profile_name = arguments.profile or store.default_for(arguments.task)
    if not profile_name:
        raise ModelMuxError(f"No default {arguments.task!r} profile; pass --profile")
    profile = store.get(profile_name)
    parameters = apply_overrides(profile.defaults, arguments.set)
    result = run_profile(
        task=arguments.task,
        profile=profile,
        input_bytes=read_input(arguments.input),
        output_path=arguments.output,
        parameters=parameters,
        emit=stderr_sink(arguments.json_events),
    )
    payload: dict[str, Any] = {
        "id": result.run_id,
        "task": arguments.task,
        "profile": profile.name,
        "output": str(result.output_path),
        "metadata": result.metadata,
    }
    print(json.dumps(payload, ensure_ascii=False) if arguments.json else result.output_path)
    return 0


def _print_runs(value: Any, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False))
        return
    records = value if isinstance(value, list) else [value]
    for record in records:
        if isinstance(record, dict):
            print(
                "\t".join(
                    str(record.get(key, ""))
                    for key in ("id", "name", "task", "profile", "status", "progress")
                )
            )


def _runs(arguments: argparse.Namespace) -> int:
    store = RunStore()
    if arguments.runs_command == "list":
        _print_runs(store.list(), json_output=arguments.json)
        return 0
    if arguments.runs_command == "show":
        _print_runs(store.get(arguments.id), json_output=arguments.json)
        return 0
    if arguments.runs_command == "rename":
        _print_runs(store.rename(arguments.id, arguments.name), json_output=arguments.json)
        return 0
    if arguments.runs_command == "delete":
        store.delete_many(arguments.ids)
        _print_runs({"deleted": arguments.ids}, json_output=arguments.json)
        return 0
    if arguments.runs_command == "cancel":
        records = [store.cancel(run_id) for run_id in arguments.ids]
        _print_runs(records, json_output=arguments.json)
        return 0
    raise ModelMuxError(f"Unknown runs command: {arguments.runs_command}")


def execute(arguments: argparse.Namespace) -> int:
    if arguments.command == "runs":
        return _runs(arguments)
    store = ProfileStore()
    if arguments.command == "profiles":
        for profile in store.all():
            print(f"{profile.name}\t{profile.task}\t{profile.adapter}\t{profile.source}")
        return 0
    if arguments.command == "inspect":
        profile = store.get(arguments.profile)
        print(yaml_dump(profile.data), end="")
        return 0
    return _run(arguments, store)


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
