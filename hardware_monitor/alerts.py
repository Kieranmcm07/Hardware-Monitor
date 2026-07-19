"""Stateful threshold alerts with hold, hysteresis and cooldown controls."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class AlertRule:
    key: str
    label: str
    threshold: float
    unit: str = "%"
    hold_seconds: float = 5.0
    hysteresis: float = 5.0
    cooldown_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("alert key cannot be empty")
        if self.hold_seconds < 0 or self.hysteresis < 0 or self.cooldown_seconds < 0:
            raise ValueError("alert timings and hysteresis cannot be negative")


@dataclass(frozen=True)
class AlertEvent:
    key: str
    label: str
    kind: str
    value: float | None
    threshold: float
    unit: str
    timestamp: float
    message: str


@dataclass
class _State:
    active: bool = False
    above_since: float | None = None
    last_raised: float | None = None


DEFAULT_RULES = (
    AlertRule("cpu", "CPU load", 90.0),
    AlertRule("memory", "Memory use", 90.0),
    AlertRule("storage", "Storage use", 90.0, hold_seconds=15.0),
    AlertRule("temperature", "Temperature", 85.0, unit="°C", hold_seconds=3.0),
)


class AlertEngine:
    """Evaluate numeric metrics without alert flicker or repeated notifications."""

    def __init__(self, rules: tuple[AlertRule, ...] | list[AlertRule] = DEFAULT_RULES) -> None:
        if len({rule.key for rule in rules}) != len(rules):
            raise ValueError("alert rule keys must be unique")
        self.rules = tuple(rules)
        self._states = {rule.key: _State() for rule in self.rules}

    @property
    def active_keys(self) -> tuple[str, ...]:
        return tuple(rule.key for rule in self.rules if self._states[rule.key].active)

    def evaluate(
        self, metrics: Mapping[str, float | int | None], timestamp: float | None = None
    ) -> tuple[AlertEvent, ...]:
        now = time.time() if timestamp is None else float(timestamp)
        events: list[AlertEvent] = []
        for rule in self.rules:
            raw_value = metrics.get(rule.key)
            if raw_value is None:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            state = self._states[rule.key]
            if state.active:
                if value <= rule.threshold - rule.hysteresis:
                    state.active = False
                    state.above_since = None
                    events.append(self._event(rule, "resolved", value, now))
                continue
            if value < rule.threshold:
                state.above_since = None
                continue
            if state.above_since is None:
                state.above_since = now
            held = now - state.above_since >= rule.hold_seconds
            cooled = state.last_raised is None or now - state.last_raised >= rule.cooldown_seconds
            if held and cooled:
                state.active = True
                state.last_raised = now
                events.append(self._event(rule, "raised", value, now))
        return tuple(events)

    def resolve_all(self, timestamp: float | None = None) -> tuple[AlertEvent, ...]:
        now = time.time() if timestamp is None else float(timestamp)
        events: list[AlertEvent] = []
        for rule in self.rules:
            state = self._states[rule.key]
            if state.active:
                state.active = False
                events.append(self._event(rule, "resolved", None, now))
            state.above_since = None
        return tuple(events)

    @staticmethod
    def _event(rule: AlertRule, kind: str, value: float | None, now: float) -> AlertEvent:
        if kind == "raised":
            message = f"{rule.label} reached {value:.1f}{rule.unit}"
        else:
            message = f"{rule.label} returned to normal"
        return AlertEvent(
            key=rule.key,
            label=rule.label,
            kind=kind,
            value=value,
            threshold=rule.threshold,
            unit=rule.unit,
            timestamp=now,
            message=message,
        )


def metrics_from_snapshot(snapshot: object, temperature_c: float | None = None) -> dict[str, float | None]:
    """Adapt a monitor Snapshot without importing it (keeps this module reusable)."""
    return {
        "cpu": getattr(snapshot, "cpu_usage_percent", None),
        "memory": getattr(snapshot, "memory_used_percent", None),
        "storage": getattr(snapshot, "disk_used_percent", None),
        "temperature": temperature_c,
    }
