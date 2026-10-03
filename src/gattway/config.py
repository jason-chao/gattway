"""Configuration: a TOML file plus environment overrides.

Search order for the file: ``GATTWAY_CONFIG``, then ``/etc/gattway/gattway.toml``,
then ``./gattway.toml``. A missing file is fine; everything has a default.

Environment overrides: ``GATTWAY_NAME``, ``GATTWAY_HOST``, ``GATTWAY_PORT``,
``GATTWAY_TOKEN``, ``GATTWAY_FAKE``.
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

DEFAULT_PORT = 7120
SYSTEM_CONFIG = Path("/etc/gattway/gattway.toml")
LOCAL_CONFIG = Path("gattway.toml")

_TRUE = {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    pass


@dataclass
class RadioConfig:
    address: str
    label: str | None = None
    enabled: bool = True
    default: bool = False


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    token: str = ""
    # Timing. Defaults follow docs/PROTOCOL.md; tests shorten them.
    hb_timeout_s: float = 6.0
    status_interval_s: float = 5.0
    farewell_gap_s: float = 0.02
    late_ms: float = 250.0
    # Resolution of the timed write loop and client watchdog.
    tick_s: float = 0.25


@dataclass
class Config:
    name: str = field(default_factory=socket.gethostname)
    server: ServerConfig = field(default_factory=ServerConfig)
    radios: list[RadioConfig] = field(default_factory=list)
    fake: bool = False
    path: Path | None = None

    @property
    def default_radio(self) -> RadioConfig | None:
        """The configured default radio, or the first enabled one."""
        for r in self.radios:
            if r.default:
                return r
        for r in self.radios:
            if r.enabled:
                return r
        return None


def normalise_address(address: str) -> str:
    return str(address).strip().lower()


def find_config_path(env: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if env is None else env
    explicit = env.get("GATTWAY_CONFIG")
    if explicit:
        return Path(explicit)
    for candidate in (SYSTEM_CONFIG, LOCAL_CONFIG):
        if candidate.is_file():
            return candidate
    return None


def parse_config(data: Mapping[str, Any], path: Path | None = None) -> Config:
    """Build a Config from a parsed TOML mapping. Pure: no environment."""
    cfg = Config(path=path)

    instance = data.get("instance", {}) or {}
    if not isinstance(instance, Mapping):
        raise ConfigError("[instance] must be a table")
    if instance.get("name"):
        cfg.name = str(instance["name"])

    server = data.get("server", {}) or {}
    if not isinstance(server, Mapping):
        raise ConfigError("[server] must be a table")
    for key in ("host", "token"):
        if key in server:
            setattr(cfg.server, key, str(server[key]))
    if "port" in server:
        cfg.server.port = _port(server["port"])
    for key in ("hb_timeout_s", "status_interval_s", "farewell_gap_s", "late_ms", "tick_s"):
        if key in server:
            try:
                setattr(cfg.server, key, float(server[key]))
            except (TypeError, ValueError):
                raise ConfigError(f"[server] {key} must be a number") from None

    radios = data.get("radio", []) or []
    if not isinstance(radios, list):
        raise ConfigError("[[radio]] must be an array of tables")
    seen: set[str] = set()
    defaults = 0
    for entry in radios:
        if not isinstance(entry, Mapping) or "address" not in entry:
            raise ConfigError("each [[radio]] needs an address")
        address = normalise_address(entry["address"])
        if address in seen:
            raise ConfigError(f"radio {address} listed twice")
        seen.add(address)
        rc = RadioConfig(
            address=address,
            label=str(entry["label"]) if entry.get("label") is not None else None,
            enabled=bool(entry.get("enabled", True)),
            default=bool(entry.get("default", False)),
        )
        if rc.default:
            defaults += 1
        cfg.radios.append(rc)
    if defaults > 1:
        raise ConfigError("at most one radio may be default")
    labels = [r.label for r in cfg.radios if r.label]
    if len(labels) != len(set(labels)):
        raise ConfigError("radio labels must be unique")

    fake = data.get("fake", {}) or {}
    if isinstance(fake, Mapping):
        cfg.fake = bool(fake.get("enabled", False))
    elif isinstance(fake, bool):
        cfg.fake = fake
    else:
        raise ConfigError("[fake] must be a table")
    return cfg


def apply_env(cfg: Config, env: Mapping[str, str]) -> Config:
    if env.get("GATTWAY_NAME"):
        cfg.name = env["GATTWAY_NAME"]
    if env.get("GATTWAY_HOST"):
        cfg.server.host = env["GATTWAY_HOST"]
    if env.get("GATTWAY_PORT"):
        cfg.server.port = _port(env["GATTWAY_PORT"])
    if "GATTWAY_TOKEN" in env:
        cfg.server.token = env["GATTWAY_TOKEN"]
    if env.get("GATTWAY_FAKE"):
        cfg.fake = env["GATTWAY_FAKE"].strip().lower() in _TRUE
    return cfg


def load_config(path: str | os.PathLike | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Load the configuration file (if any) and apply environment overrides."""
    env = os.environ if env is None else env
    p = Path(path) if path is not None else find_config_path(env)
    if p is not None:
        try:
            with open(p, "rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {p}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{p}: {exc}") from None
        cfg = parse_config(data, p)
    else:
        cfg = Config()
    return apply_env(cfg, env)


def _port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"port must be an integer, not {value!r}") from None
    if not 0 <= port <= 65535:
        raise ConfigError(f"port out of range: {port}")
    return port
