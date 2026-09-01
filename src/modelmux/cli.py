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


TASK_ALIASES = ("tts", "asr", "chat", "image", "embed")


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
        "task": arguments.task,
        "profile": profile.name,
        "output": str(result.output_path),
        "metadata": result.metadata,
    }
    print(json.dumps(payload, ensure_ascii=False) if arguments.json else result.output_path)
    return 0


def execute(arguments: argparse.Namespace) -> int:
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
