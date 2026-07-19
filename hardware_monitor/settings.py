"""Validated, atomic application settings for NEXUS Hardware Monitor v4."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


SETTINGS_VERSION = 1
ACCENT_PRESETS = {"red", "crimson", "ruby", "mono"}
METRICS = {"cpu", "memory", "storage", "network", "temperature", "battery"}


def _posix_app_directory(app_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(app_name).casefold()).strip("-")
    return slug or "nexus-hardware-monitor"


def config_directory(app_name: str = "NEXUS Hardware Monitor") -> Path:
    """Return a per-user config directory without creating it."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / app_name
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / _posix_app_directory(app_name)


def data_directory(app_name: str = "NEXUS Hardware Monitor") -> Path:
    """Return a per-user persistent data directory without creating it."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / app_name
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / _posix_app_directory(app_name)


def _boolean(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(high, max(low, number))


@dataclass(frozen=True)
class AppSettings:
    version: int = SETTINGS_VERSION
    refresh_seconds: float = 1.0
    history_enabled: bool = True
    history_days: int = 30
    alerts_enabled: bool = True
    cpu_alert_percent: float = 90.0
    memory_alert_percent: float = 90.0
    storage_alert_percent: float = 90.0
    temperature_alert_c: float = 85.0
    accent: str = "red"
    reduced_motion: bool = False
    animation_speed: float = 1.0
    dashboard_metrics: tuple[str, ...] = field(
        default_factory=lambda: ("cpu", "memory", "storage", "network")
    )
    minimize_to_tray: bool = False
    start_in_tray: bool = False
    overlay_enabled: bool = False
    overlay_opacity: float = 0.92
    diagnostics_host: str = "1.1.1.1"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "AppSettings":
        values = values or {}
        accent = str(values.get("accent", "red")).lower()
        if accent not in ACCENT_PRESETS:
            accent = "red"
        raw_metrics = values.get("dashboard_metrics", cls().dashboard_metrics)
        if not isinstance(raw_metrics, (list, tuple)):
            raw_metrics = cls().dashboard_metrics
        metrics = tuple(dict.fromkeys(str(v).lower() for v in raw_metrics if str(v).lower() in METRICS))
        if not metrics:
            metrics = cls().dashboard_metrics
        host = str(values.get("diagnostics_host", "1.1.1.1")).strip()[:253]
        if not host or any(character.isspace() for character in host):
            host = "1.1.1.1"
        return cls(
            version=SETTINGS_VERSION,
            refresh_seconds=_number(values.get("refresh_seconds"), 1.0, 0.25, 30.0),
            history_enabled=_boolean(values.get("history_enabled"), True),
            history_days=round(_number(values.get("history_days"), 30, 1, 365)),
            alerts_enabled=_boolean(values.get("alerts_enabled"), True),
            cpu_alert_percent=_number(values.get("cpu_alert_percent"), 90, 50, 100),
            memory_alert_percent=_number(values.get("memory_alert_percent"), 90, 50, 100),
            storage_alert_percent=_number(values.get("storage_alert_percent"), 90, 50, 100),
            temperature_alert_c=_number(values.get("temperature_alert_c"), 85, 40, 120),
            accent=accent,
            reduced_motion=_boolean(values.get("reduced_motion"), False),
            animation_speed=_number(values.get("animation_speed"), 1, 0.25, 3),
            dashboard_metrics=metrics,
            minimize_to_tray=_boolean(values.get("minimize_to_tray"), False),
            start_in_tray=_boolean(values.get("start_in_tray"), False),
            overlay_enabled=_boolean(values.get("overlay_enabled"), False),
            overlay_opacity=_number(values.get("overlay_opacity"), 0.92, 0.35, 1),
            diagnostics_host=host,
        )

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["dashboard_metrics"] = list(self.dashboard_metrics)
        return result


class SettingsStore:
    """Load and atomically save settings; malformed files safely use defaults."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else config_directory() / "settings.json"

    def load(self) -> AppSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return AppSettings()
            return AppSettings.from_mapping(raw)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        validated = AppSettings.from_mapping(settings.as_dict())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False, suffix=".tmp"
            ) as handle:
                temporary = Path(handle.name)
                json.dump(validated.as_dict(), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
