from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from modelmux.errors import ModelMuxError


def config_home() -> Path:
    configured = os.environ.get("MODELMUX_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg).expanduser() / "modelmux" if xdg else Path.home() / ".config" / "modelmux"


def cache_home() -> Path:
    configured = os.environ.get("MODELMUX_CACHE_HOME")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "modelmux"
    if sys_platform() == "darwin":
        return Path.home() / "Library" / "Caches" / "modelmux"
    return Path.home() / ".cache" / "modelmux"


def sys_platform() -> str:
    import sys

    return sys.platform


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        contents = path.read_text(encoding="utf-8")
        value = json.loads(contents) if path.suffix.lower() == ".json" else yaml.safe_load(contents)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as error:
        raise ModelMuxError(f"Cannot read configuration {path}: {error}") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ModelMuxError(f"Configuration must be a mapping: {path}")
    return value


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


@dataclass(frozen=True)
class Profile:
    name: str
    source: str
    data: dict[str, Any]

    @property
    def task(self) -> str:
        return str(self.data.get("task", ""))

    @property
    def adapter(self) -> str:
        return str(self.data.get("adapter", ""))

    @property
    def defaults(self) -> dict[str, Any]:
        value = self.data.get("defaults", {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def extension(self) -> str:
        value = str(self.data.get("output", {}).get("extension", ".out"))
        return value if value.startswith(".") else f".{value}"

    @property
    def input_extension(self) -> str:
        value = str(self.data.get("input", {}).get("extension", ".input"))
        return value if value.startswith(".") else f".{value}"


@dataclass(frozen=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    concurrency: int = 1
    model_loading: str = "lazy"
    preload: tuple[str, ...] = ()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def server_settings(root: Path | None = None) -> ServerSettings:
    raw = ProfileStore(root).user_config().get("server", {})
    if not isinstance(raw, dict):
        raise ModelMuxError("config server must be a mapping")
    host = str(raw.get("host", "127.0.0.1"))
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ModelMuxError("ModelMux server may only listen on localhost")
    try:
        port = int(raw.get("port", 8765))
        concurrency = int(raw.get("concurrency", 1))
    except (TypeError, ValueError) as error:
        raise ModelMuxError("server port and concurrency must be integers") from error
    if not 1 <= port <= 65535:
        raise ModelMuxError("server port must be between 1 and 65535")
    if concurrency < 1:
        raise ModelMuxError("server concurrency must be at least 1")
    loading = str(raw.get("model_loading", "lazy"))
    if loading not in {"ephemeral", "lazy", "preload"}:
        raise ModelMuxError("server model_loading must be ephemeral, lazy, or preload")
    preload = raw.get("preload", [])
    if not isinstance(preload, list) or not all(isinstance(item, str) for item in preload):
        raise ModelMuxError("server preload must be a list of profile names")
    return ServerSettings(host, port, concurrency, loading, tuple(preload))


class ProfileStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or config_home()

    def user_config(self) -> dict[str, Any]:
        for name in ("config.yaml", "config.yml", "config.json"):
            path = self.root / name
            if path.is_file():
                return _load_mapping(path)
        return {}

    def _builtins(self) -> dict[str, tuple[str, dict[str, Any]]]:
        result: dict[str, tuple[str, dict[str, Any]]] = {}
        directory = resources.files("modelmux").joinpath("profiles")
        for item in directory.iterdir():
            if item.name.endswith((".yaml", ".yml", ".json")):
                with resources.as_file(item) as path:
                    data = _load_mapping(path)
                name = str(data.get("name") or Path(item.name).stem)
                result[name] = (f"builtin:{item.name}", data)
        return result

    def _user_profiles(self) -> dict[str, tuple[str, dict[str, Any]]]:
        result: dict[str, tuple[str, dict[str, Any]]] = {}
        directory = self.root / "profiles"
        if not directory.is_dir():
            return result
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in {".yaml", ".yml", ".json"}:
                continue
            data = _load_mapping(path)
            name = str(data.get("name") or path.stem)
            result[name] = (str(path), data)
        return result

    def all(self) -> list[Profile]:
        profiles = self._builtins()
        profiles.update(self._user_profiles())
        config = self.user_config()
        overrides = config.get("profiles", {})
        if overrides and not isinstance(overrides, dict):
            raise ModelMuxError("config profiles must be a mapping")
        result: list[Profile] = []
        for name, (source, data) in profiles.items():
            override = overrides.get(name, {}) if isinstance(overrides, dict) else {}
            if not isinstance(override, dict):
                raise ModelMuxError(f"Profile override must be a mapping: {name}")
            merged = deep_merge(data, override)
            self._validate(name, merged)
            result.append(Profile(name=name, source=source, data=merged))
        return sorted(result, key=lambda item: item.name)

    def get(self, name_or_path: str) -> Profile:
        path = Path(name_or_path).expanduser()
        if path.is_file():
            data = _load_mapping(path)
            name = str(data.get("name") or path.stem)
            self._validate(name, data)
            return Profile(name=name, source=str(path), data=data)
        for profile in self.all():
            if profile.name == name_or_path:
                return profile
        raise ModelMuxError(f"Unknown profile: {name_or_path}")

    def default_for(self, task: str) -> str | None:
        defaults = self.user_config().get("defaults", {})
        if isinstance(defaults, dict) and task in defaults:
            return str(defaults[task])
        matches = [profile.name for profile in self.all() if profile.task == task]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _validate(name: str, data: dict[str, Any]) -> None:
        for field in ("task", "adapter"):
            if not data.get(field):
                raise ModelMuxError(f"Profile {name!r} is missing {field!r}")
        if not isinstance(data.get("defaults", {}), dict):
            raise ModelMuxError(f"Profile {name!r} defaults must be a mapping")


def parse_override(raw: str) -> tuple[str, Any]:
    if "=" not in raw:
        raise ModelMuxError(f"Override must be KEY=VALUE: {raw}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise ModelMuxError("Override key cannot be empty")
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError as error:
        raise ModelMuxError(f"Invalid override value for {key}: {error}") from error
    return key, parsed


def apply_overrides(defaults: dict[str, Any], raw_values: list[str]) -> dict[str, Any]:
    result = deepcopy(defaults)
    for raw in raw_values:
        key, value = parse_override(raw)
        cursor = result
        parts = key.split(".")
        for part in parts[:-1]:
            existing = cursor.setdefault(part, {})
            if not isinstance(existing, dict):
                raise ModelMuxError(f"Cannot set nested value below {part!r}")
            cursor = existing
        cursor[parts[-1]] = value
    return result
