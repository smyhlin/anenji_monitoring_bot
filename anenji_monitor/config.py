from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """config.toml is missing or malformed."""


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    admin_ids: tuple[int, ...]
    moderator_ids: tuple[int, ...] = ()
    default_chats: tuple[int, ...] = ()

    @property
    def member_ids(self) -> tuple[int, ...]:
        """Everyone allowed to read data: admins plus moderators."""
        return self.admin_ids + tuple(
            user_id for user_id in self.moderator_ids if user_id not in self.admin_ids
        )


@dataclass(frozen=True)
class InverterConfig:
    host: str
    register_map: str = "registers.toml"
    listen_port: int = 8899
    poll_interval: float = 10.0
    connect_timeout: float = 30.0
    request_timeout: float = 5.0
    max_gap: int = 20
    max_batch: int = 50


@dataclass(frozen=True)
class StorageConfig:
    path: str = "state.db"


@dataclass(frozen=True)
class EventsConfig:
    confirm_samples: int = 2


@dataclass(frozen=True)
class BatteryConfig:
    soc_source: str = "voltage"
    curve: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class RecoveryConfig:
    reboot_after: float = 300.0
    drained_after: float = 1800.0


@dataclass(frozen=True)
class AlertsConfig:
    enabled: bool = True
    offline_after: float = 180.0
    soc_levels: tuple[int, ...] = (50, 25)


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    inverter: InverterConfig
    alerts: AlertsConfig
    storage: StorageConfig = field(default_factory=StorageConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    battery: BatteryConfig = field(default_factory=BatteryConfig)


def _as_ids(value: object) -> tuple[int, ...]:
    if isinstance(value, int):
        return (value,)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(int(item) for item in value)


def load(path: str | Path) -> Config:
    config_path = Path(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{config_path} not found; copy config.example.toml to config.toml") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc

    telegram = raw.get("telegram", {})
    token = str(telegram.get("token", "")).strip()
    if not token:
        raise ConfigError("[telegram] token is required")
    admin_ids = _as_ids(telegram.get("admin_ids", []))
    if not admin_ids:
        raise ConfigError("[telegram] admin_ids must list at least one user id")

    inverter = raw.get("inverter", {})
    host = str(inverter.get("host", "")).strip()
    if not host:
        raise ConfigError("[inverter] host is required")

    alerts = raw.get("alerts", {})
    recovery = raw.get("recovery", {})
    return Config(
        telegram=TelegramConfig(
            token=token,
            admin_ids=admin_ids,
            moderator_ids=_as_ids(telegram.get("moderator_ids", [])),
            default_chats=_as_ids(telegram.get("default_chats", [])),
        ),
        inverter=InverterConfig(
            host=host,
            register_map=str(inverter.get("register_map", "registers.toml")),
            listen_port=int(inverter.get("listen_port", 8899)),
            poll_interval=float(inverter.get("poll_interval", 10.0)),
            connect_timeout=float(inverter.get("connect_timeout", 30.0)),
            request_timeout=float(inverter.get("request_timeout", 5.0)),
            max_gap=int(inverter.get("max_gap", 20)),
            max_batch=int(inverter.get("max_batch", 50)),
        ),
        battery=BatteryConfig(
            soc_source=str(raw.get("battery", {}).get("soc_source", "voltage")),
            curve=tuple(
                (float(point[0]), float(point[1]))
                for point in raw.get("battery", {}).get("curve", ())
            ),
        ),
        storage=StorageConfig(path=str(raw.get("storage", {}).get("path", "state.db"))),
        events=EventsConfig(
            confirm_samples=int(raw.get("events", {}).get("confirm_samples", 2))
        ),
        recovery=RecoveryConfig(
            reboot_after=float(recovery.get("reboot_after", 300.0)),
            drained_after=float(recovery.get("drained_after", 1800.0)),
        ),
        alerts=AlertsConfig(
            enabled=bool(alerts.get("enabled", True)),
            offline_after=float(alerts.get("offline_after", 180.0)),
            soc_levels=tuple(
                sorted((int(level) for level in alerts.get("soc_levels", (50, 25))), reverse=True)
            ),
        ),
    )
