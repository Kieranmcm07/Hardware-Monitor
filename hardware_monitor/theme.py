"""Semantic black, white, and red themes for NEXUS Hardware Monitor.

The module deliberately has no Tk dependency.  It also owns the small, pure
helpers used by the custom-dashboard editor so settings can be validated before
widgets are rebuilt.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields, replace
from typing import Iterable, Mapping, Sequence


DEFAULT_THEME = "graphite"
DEFAULT_ACCENT = "#f23d52"
_HEX = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

ACCENT_ALIASES: Mapping[str, str] = {
    "red": DEFAULT_ACCENT,
    "crimson": "#d92743",
    "ruby": "#e21849",
    "scarlet": "#ff334f",
    "white": "#f7f7f8",
    "mono": "#f7f7f8",
}

DASHBOARD_METRICS = (
    "cpu",
    "memory",
    "storage",
    "network",
    "temperature",
    "battery",
)
DEFAULT_DASHBOARD_METRICS = ("cpu", "memory", "storage")
METRIC_LABELS: Mapping[str, str] = {
    "cpu": "CPU",
    "memory": "Memory",
    "storage": "Storage",
    "network": "Network",
    "temperature": "Temperature",
    "battery": "Battery",
}
_METRIC_ALIASES = {
    "ram": "memory",
    "disk": "storage",
    "drive": "storage",
    "net": "network",
    "temp": "temperature",
    "power": "battery",
}


def normalize_hex(value: object) -> str | None:
    """Return lowercase ``#rrggbb`` or ``None`` for malformed input."""
    if not isinstance(value, str):
        return None
    source = value.strip()
    if not _HEX.fullmatch(source):
        return None
    digits = source.removeprefix("#").lower()
    if len(digits) == 3:
        digits = "".join(character * 2 for character in digits)
    return f"#{digits}"


# Compatibility name for callers that prefer British spelling in the API.
normalize_hex_colour = normalize_hex


def _rgb(value: str) -> tuple[int, int, int]:
    colour = normalize_hex(value)
    if colour is None:
        raise ValueError(f"invalid RGB colour: {value!r}")
    return tuple(int(colour[index:index + 2], 16) for index in (1, 3, 5))


def mix_colour(first: str, second: str, amount: float) -> str:
    """Linearly blend two RGB colours with a clamped mix amount."""
    try:
        ratio = float(amount)
    except (TypeError, ValueError):
        ratio = 0.0
    if not math.isfinite(ratio):
        ratio = 0.0
    ratio = max(0.0, min(1.0, ratio))
    values = tuple(
        round(start + (end - start) * ratio)
        for start, end in zip(_rgb(first), _rgb(second))
    )
    return "#" + "".join(f"{value:02x}" for value in values)


def relative_luminance(colour: str) -> float:
    channels: list[float] = []
    for value in _rgb(colour):
        component = value / 255.0
        channels.append(
            component / 12.92
            if component <= 0.04045
            else ((component + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return red * 0.2126 + green * 0.7152 + blue * 0.0722


def contrast_ratio(first: str, second: str) -> float:
    first_luminance = relative_luminance(first)
    second_luminance = relative_luminance(second)
    light = max(first_luminance, second_luminance)
    dark = min(first_luminance, second_luminance)
    return (light + 0.05) / (dark + 0.05)


def validate_accent(
    value: object,
    *,
    background: str = "#030304",
    fallback: str = DEFAULT_ACCENT,
    minimum_contrast: float = 3.0,
) -> str:
    """Normalize a custom accent and reject invisible or corrupt values."""
    candidate: object = value
    if isinstance(value, str):
        candidate = ACCENT_ALIASES.get(value.strip().casefold(), value)
    normalized = normalize_hex(candidate)
    safe_background = normalize_hex(background) or "#030304"
    safe_fallback = normalize_hex(fallback) or DEFAULT_ACCENT
    try:
        threshold = float(minimum_contrast)
    except (TypeError, ValueError):
        threshold = 3.0
    if not math.isfinite(threshold):
        threshold = 3.0
    if normalized is None:
        return safe_fallback
    if contrast_ratio(normalized, safe_background) < max(1.0, threshold):
        return safe_fallback
    return normalized


@dataclass(frozen=True, slots=True)
class SemanticTheme:
    key: str
    name: str
    background: str
    panel: str
    surface: str
    surface_alt: str
    border: str
    border_strong: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_dim: str
    danger: str
    success: str
    warning: str
    track: str
    grid: str
    graph_fill: str

    def with_accent(self, value: object) -> "SemanticTheme":
        accent = validate_accent(value, background=self.background, fallback=self.accent)
        hover_target = (
            "#ffffff" if relative_luminance(self.background) < 0.35 else "#000000"
        )
        return replace(
            self,
            accent=accent,
            accent_hover=mix_colour(accent, hover_target, 0.18),
            accent_dim=mix_colour(accent, self.background, 0.62),
            graph_fill=mix_colour(accent, self.background, 0.78),
        )

    def as_dict(self) -> dict[str, str]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


THEME_PRESETS: Mapping[str, SemanticTheme] = {
    "graphite": SemanticTheme(
        "graphite", "Graphite Red", "#030304", "#09090b", "#111113",
        "#18181b", "#35353b", "#686870", "#f7f7f8", "#a4a4ac",
        "#f23d52", "#ff6374", "#68131f", "#ff4258", "#d7d7db",
        "#ff8b98", "#252529", "#242428", "#28090f",
    ),
    "pitch_black": SemanticTheme(
        "pitch_black", "Pitch Black", "#000000", "#050506", "#0b0b0d",
        "#141416", "#323237", "#74747b", "#ffffff", "#ababaf",
        "#ff334f", "#ff667a", "#5f0b18", "#ff334f", "#e2e2e4",
        "#ff8b99", "#202023", "#1d1d20", "#2b070d",
    ),
    "paper": SemanticTheme(
        "paper", "Paper and Ink", "#f4f4f1", "#ffffff", "#ffffff",
        "#e9e9e6", "#b9b9b5", "#3c3c40", "#111113", "#626267",
        "#c81732", "#a80f27", "#edc4cb", "#b40e29", "#242427",
        "#8f172a", "#d7d7d3", "#dededa", "#f2d8dd",
    ),
}


def resolve_theme(preset: object = DEFAULT_THEME, accent: object | None = None) -> SemanticTheme:
    key = str(preset).strip().casefold()
    theme = THEME_PRESETS.get(key, THEME_PRESETS[DEFAULT_THEME])
    return theme if accent is None else theme.with_accent(accent)


def _metric_key(value: object) -> str:
    key = str(value).strip().casefold()
    return _METRIC_ALIASES.get(key, key)


def normalize_metric_order(
    metrics: Iterable[object] | object,
    *,
    available: Sequence[str] = DASHBOARD_METRICS,
    fallback: Sequence[str] = DEFAULT_DASHBOARD_METRICS,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Remove unknowns/duplicates without changing the user's valid order."""
    allowed = tuple(dict.fromkeys(_metric_key(item) for item in available))
    source = (metrics,) if isinstance(metrics, str) or not isinstance(metrics, Iterable) else metrics
    result: list[str] = []
    for item in source:
        key = _metric_key(item)
        if key in allowed and key not in result:
            result.append(key)
    if result or allow_empty:
        return tuple(result)
    defaults = [_metric_key(item) for item in fallback]
    return tuple(dict.fromkeys(key for key in defaults if key in allowed))


def set_metric_enabled(
    metrics: Iterable[object],
    metric: object,
    enabled: bool,
    *,
    available: Sequence[str] = DASHBOARD_METRICS,
    minimum: int = 1,
) -> tuple[str, ...]:
    allowed = tuple(dict.fromkeys(_metric_key(item) for item in available))
    key = _metric_key(metric)
    if key not in allowed:
        raise ValueError(f"unknown dashboard metric: {metric!r}")
    order = list(normalize_metric_order(metrics, available=allowed, allow_empty=True))
    if enabled:
        if key not in order:
            order.append(key)
    elif key in order and len(order) > max(0, int(minimum)):
        order.remove(key)
    return tuple(order)


def toggle_metric(
    metrics: Iterable[object],
    metric: object,
    *,
    available: Sequence[str] = DASHBOARD_METRICS,
    minimum: int = 1,
) -> tuple[str, ...]:
    current = normalize_metric_order(metrics, available=available, allow_empty=True)
    key = _metric_key(metric)
    return set_metric_enabled(
        current, key, key not in current, available=available, minimum=minimum
    )


def move_metric(
    metrics: Iterable[object],
    metric: object,
    offset: int,
    *,
    available: Sequence[str] = DASHBOARD_METRICS,
) -> tuple[str, ...]:
    order = list(normalize_metric_order(metrics, available=available, allow_empty=True))
    key = _metric_key(metric)
    if key not in order:
        raise ValueError(f"metric is not enabled: {metric!r}")
    old_index = order.index(key)
    new_index = max(0, min(len(order) - 1, old_index + int(offset)))
    if new_index != old_index:
        order.pop(old_index)
        order.insert(new_index, key)
    return tuple(order)


__all__ = [
    "ACCENT_ALIASES", "DASHBOARD_METRICS", "DEFAULT_ACCENT",
    "DEFAULT_DASHBOARD_METRICS", "DEFAULT_THEME", "METRIC_LABELS",
    "SemanticTheme", "THEME_PRESETS", "contrast_ratio", "mix_colour",
    "move_metric", "normalize_hex", "normalize_hex_colour",
    "normalize_metric_order", "relative_luminance", "resolve_theme",
    "set_metric_enabled", "toggle_metric", "validate_accent",
]
