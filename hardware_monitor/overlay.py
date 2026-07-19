"""Collector-free, cross-platform NEXUS gaming telemetry overlay.

The overlay only consumes snapshots already cached by the dashboard.  It never
starts a collector, probes hardware, or sends network traffic of its own.
"""

from __future__ import annotations

import math
import re
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Mapping

try:  # Package destination.
    from .theme import SemanticTheme, mix_colour, resolve_theme
except ImportError:  # Standalone staging file.
    from v4_theme import SemanticTheme, mix_colour, resolve_theme


OVERLAY_METRICS = ("cpu", "memory", "storage", "network", "temperature", "battery")
DEFAULT_OVERLAY_METRICS = ("cpu", "memory", "storage", "network")
_ALIASES = {"ram": "memory", "disk": "storage", "net": "network", "temp": "temperature"}
_GEOMETRY = re.compile(
    r"^\s*(?P<width>\d+)x(?P<height>\d+)"
    r"(?P<x>[+-]\d+)(?P<y>[+-]\d+)\s*$"
)
_MISSING = object()


@dataclass(frozen=True, slots=True)
class ScreenBounds:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("screen bounds must have positive area")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True, slots=True)
class WindowGeometry:
    width: int
    height: int
    x: int
    y: int

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError("window geometry must have positive size")

    def as_tk(self) -> str:
        return geometry_string(self.width, self.height, self.x, self.y)


def geometry_string(width: int, height: int, x: int, y: int) -> str:
    return f"{max(1, int(width))}x{max(1, int(height))}{int(x):+d}{int(y):+d}"


def parse_geometry(value: str) -> WindowGeometry:
    match = _GEOMETRY.fullmatch(str(value))
    if match is None:
        raise ValueError(f"invalid window geometry: {value!r}")
    return WindowGeometry(
        int(match.group("width")), int(match.group("height")),
        int(match.group("x")), int(match.group("y")),
    )


def fit_geometry(
    width: int,
    height: int,
    x: int,
    y: int,
    bounds: ScreenBounds,
    *,
    margin: int = 10,
) -> WindowGeometry:
    """Clamp a window to a virtual display, shrinking only when necessary."""
    margin = max(0, int(margin))
    margin_x = min(margin, max(0, (bounds.width - 1) // 2))
    margin_y = min(margin, max(0, (bounds.height - 1) // 2))
    available_width = max(1, bounds.width - margin_x * 2)
    available_height = max(1, bounds.height - margin_y * 2)
    width = min(max(1, int(width)), available_width)
    height = min(max(1, int(height)), available_height)
    minimum_x, minimum_y = bounds.left + margin_x, bounds.top + margin_y
    maximum_x = bounds.right - margin_x - width
    maximum_y = bounds.bottom - margin_y - height
    return WindowGeometry(
        width, height,
        max(minimum_x, min(int(x), maximum_x)),
        max(minimum_y, min(int(y), maximum_y)),
    )


def top_right_geometry(
    width: int,
    height: int,
    bounds: ScreenBounds,
    *,
    margin: int = 24,
) -> WindowGeometry:
    margin = max(0, int(margin))
    return fit_geometry(
        width, height, bounds.right - int(width) - margin, bounds.top + margin,
        bounds, margin=margin,
    )


def clamp_opacity(value: object, fallback: float = 0.90) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = fallback
    if not math.isfinite(result):
        result = fallback
    return max(0.35, min(1.0, result))


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def format_percent(value: object) -> str:
    result = _number(value)
    if result is None:
        return "N/A"
    result = max(0.0, min(100.0, result))
    decimals = 0 if abs(result - round(result)) < 0.05 else 1
    return f"{result:.{decimals}f}%"


def format_temperature(value: object) -> str:
    result = _number(value)
    return "N/A" if result is None else f"{result:.0f}°C"


def format_rate(value: object) -> str:
    result = _number(value)
    if result is None:
        return "N/A"
    amount = max(0.0, result)
    units = ("B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s")
    unit = units[0]
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            break
        amount /= 1024.0
    decimals = 0 if amount >= 100 else 1
    return f"{amount:.{decimals}f} {unit}"


def normalize_overlay_metrics(metrics: Iterable[object] | object) -> tuple[str, ...]:
    source = (metrics,) if isinstance(metrics, str) or not isinstance(metrics, Iterable) else metrics
    result: list[str] = []
    for item in source:
        key = str(item).strip().casefold()
        key = _ALIASES.get(key, key)
        if key in OVERLAY_METRICS and key not in result:
            result.append(key)
    return tuple(result) or DEFAULT_OVERLAY_METRICS


def _read(source: object, *names: str) -> object | None:
    if source is None:
        return None
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        try:
            return getattr(source, name)
        except (AttributeError, TypeError):
            continue
    return None


@dataclass(frozen=True, slots=True)
class OverlayTelemetry:
    cpu_percent: float | None = None
    memory_percent: float | None = None
    storage_percent: float | None = None
    download_bps: float | None = None
    upload_bps: float | None = None
    temperature_c: float | None = None
    battery_percent: float | None = None
    captured_at: float | None = None

    @classmethod
    def from_cached(
        cls,
        snapshot: object = None,
        network: object = None,
        temperature_c: object = None,
    ) -> "OverlayTelemetry":
        if isinstance(snapshot, cls) and network is None and temperature_c is None:
            return snapshot
        temperature = _number(temperature_c)
        if temperature is None:
            candidates = (
                _read(snapshot, "temperature_c"),
                _read(snapshot, "cpu_temperature_c"),
                _read(snapshot, "gpu_temperature_c"),
                _read(snapshot, "max_temperature_c"),
            )
            temperatures = [value for item in candidates if (value := _number(item)) is not None]
            temperature = max(temperatures, default=None)
        return cls(
            cpu_percent=_number(_read(snapshot, "cpu_percent", "cpu_usage_percent")),
            memory_percent=_number(_read(snapshot, "memory_percent", "memory_used_percent")),
            storage_percent=_number(_read(snapshot, "storage_percent", "disk_used_percent")),
            download_bps=_number(
                _read(network, "download_bps") if network is not None
                else _read(snapshot, "download_bps")
            ),
            upload_bps=_number(
                _read(network, "upload_bps") if network is not None
                else _read(snapshot, "upload_bps")
            ),
            temperature_c=temperature,
            battery_percent=_number(_read(snapshot, "battery_percent")),
            captured_at=_number(_read(snapshot, "captured_at", "timestamp", "updated_at")),
        )


@dataclass(frozen=True, slots=True)
class MetricDisplay:
    key: str
    label: str
    value: str
    detail: str


def metric_display(metric: str, telemetry: OverlayTelemetry) -> MetricDisplay:
    key = str(metric).strip().casefold()
    if key == "cpu":
        return MetricDisplay(key, "CPU", format_percent(telemetry.cpu_percent), "PROCESSOR LOAD")
    if key == "memory":
        return MetricDisplay(key, "MEMORY", format_percent(telemetry.memory_percent), "MEMORY USED")
    if key == "storage":
        return MetricDisplay(key, "STORAGE", format_percent(telemetry.storage_percent), "SYSTEM VOLUME")
    if key == "network":
        return MetricDisplay(
            key, "NETWORK DOWN", format_rate(telemetry.download_bps),
            f"UP {format_rate(telemetry.upload_bps)}",
        )
    if key == "temperature":
        return MetricDisplay(
            key, "TEMPERATURE", format_temperature(telemetry.temperature_c),
            "HOTTEST AVAILABLE SENSOR",
        )
    if key == "battery":
        return MetricDisplay(
            key, "BATTERY", format_percent(telemetry.battery_percent),
            "CHARGE REMAINING",
        )
    raise ValueError(f"unknown overlay metric: {metric!r}")


def _rounded(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    **options,
) -> int:
    radius = max(2.0, min(radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    points = (
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    )
    return canvas.create_polygon(points, smooth=True, splinesteps=28, **options)


class GamingOverlay(tk.Toplevel):
    """Draggable telemetry Toplevel controlled by the main Tk thread."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        metrics: Iterable[object] = DEFAULT_OVERLAY_METRICS,
        opacity: float = 0.90,
        topmost: bool = True,
        theme: SemanticTheme | Mapping[str, str] | None = None,
        reduced_motion: bool = False,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.withdraw()
        self.title("NEXUS Performance Overlay")
        self._metrics = normalize_overlay_metrics(metrics)
        self._telemetry = OverlayTelemetry()
        self._opacity = clamp_opacity(opacity)
        self._topmost = bool(topmost)
        self._theme = theme or resolve_theme()
        self._reduced_motion = bool(reduced_motion)
        self._on_close = on_close
        self._closed = False
        self._positioned = False
        self._drag_offset = (0, 0)
        self._phase = 0.0
        self._animation_job: str | None = None

        width, height = self._preferred_size()
        self.geometry(geometry_string(width, height, 20, 20))
        self.resizable(False, False)
        self.configure(bg=self._colour("background"))
        try:
            self.overrideredirect(True)
        except tk.TclError:
            pass
        self._apply_attributes()

        self.canvas = tk.Canvas(
            self, bg=self._colour("background"), bd=0,
            highlightthickness=0, cursor="fleur",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self._draw())
        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.tag_bind("close", "<Button-1>", lambda _event: self.close())
        self.bind("<Escape>", lambda _event: self.hide())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Destroy>", self._destroyed, add="+")

    def _colour(self, role: str) -> str:
        value = (
            self._theme.get(role) if isinstance(self._theme, Mapping)
            else getattr(self._theme, role, None)
        )
        return value if isinstance(value, str) else getattr(resolve_theme(), role)

    def _preferred_size(self) -> tuple[int, int]:
        return max(430, 34 + len(self._metrics) * 132), 154

    def _screen_bounds(self) -> ScreenBounds:
        try:
            left, top = int(self.winfo_vrootx()), int(self.winfo_vrooty())
            width, height = int(self.winfo_vrootwidth()), int(self.winfo_vrootheight())
            if width > 0 and height > 0:
                return ScreenBounds(left, top, left + width, top + height)
        except (tk.TclError, ValueError):
            pass
        return ScreenBounds(0, 0, self.winfo_screenwidth(), self.winfo_screenheight())

    def _apply_attributes(self) -> None:
        for name, value in (("-topmost", self._topmost), ("-alpha", self._opacity)):
            try:
                self.attributes(name, value)
            except tk.TclError:
                pass

    @property
    def visible(self) -> bool:
        if self._closed:
            return False
        try:
            return self.state() not in {"withdrawn", "iconic"}
        except tk.TclError:
            return False

    @property
    def telemetry(self) -> OverlayTelemetry:
        return self._telemetry

    @property
    def metrics(self) -> tuple[str, ...]:
        return self._metrics

    def show(self) -> bool:
        if self._closed:
            return False
        self.update_idletasks()
        if not self._positioned:
            width, height = self._preferred_size()
            self.geometry(top_right_geometry(width, height, self._screen_bounds()).as_tk())
            self._positioned = True
        self._apply_attributes()
        try:
            self.deiconify()
            self.lift()
        except tk.TclError:
            return False
        self._draw()
        self._schedule_animation()
        return True

    def hide(self) -> bool:
        if self._closed:
            return False
        self._cancel_animation()
        try:
            self.withdraw()
        except tk.TclError:
            return False
        return True

    def toggle(self) -> bool:
        if self.visible:
            self.hide()
            return False
        return self.show()

    def update(  # type: ignore[override]
        self,
        telemetry: object = _MISSING,
        *,
        snapshot: object = None,
        network: object = None,
        temperature_c: object = None,
    ) -> object:
        """Update telemetry, while preserving Tk's no-argument ``update``."""
        if telemetry is _MISSING and snapshot is None and network is None and temperature_c is None:
            return super().update()
        if telemetry is not _MISSING and snapshot is not None:
            raise ValueError("pass telemetry or snapshot, not both")
        source = snapshot if telemetry is _MISSING else telemetry
        return self.update_telemetry(source, network=network, temperature_c=temperature_c)

    def update_telemetry(
        self,
        cached: object = None,
        *,
        network: object = None,
        temperature_c: object = None,
    ) -> OverlayTelemetry:
        self._telemetry = OverlayTelemetry.from_cached(cached, network, temperature_c)
        if not self._closed:
            self._draw()
        return self._telemetry

    def set_metrics(self, metrics: Iterable[object]) -> tuple[str, ...]:
        self._metrics = normalize_overlay_metrics(metrics)
        if not self._closed:
            width, height = self._preferred_size()
            geometry = fit_geometry(
                width, height, self.winfo_x(), self.winfo_y(), self._screen_bounds()
            )
            self.geometry(geometry.as_tk())
            self._draw()
        return self._metrics

    def set_opacity(self, opacity: object) -> float:
        self._opacity = clamp_opacity(opacity)
        self._apply_attributes()
        return self._opacity

    def set_topmost(self, enabled: bool) -> None:
        self._topmost = bool(enabled)
        self._apply_attributes()

    def set_reduced_motion(self, enabled: bool) -> None:
        self._reduced_motion = bool(enabled)
        if enabled:
            self._cancel_animation()
            self._phase = 0.0
            self._draw()
        elif self.visible:
            self._schedule_animation()

    def set_theme(self, theme: SemanticTheme | Mapping[str, str]) -> None:
        self._theme = theme
        self.configure(bg=self._colour("background"))
        self.canvas.configure(bg=self._colour("background"))
        self._draw()

    def _start_drag(self, event: tk.Event) -> None:
        if "close" in self.canvas.gettags("current"):
            return
        self._drag_offset = event.x_root - self.winfo_x(), event.y_root - self.winfo_y()

    def _drag(self, event: tk.Event) -> None:
        if self._closed:
            return
        geometry = fit_geometry(
            self.winfo_width(), self.winfo_height(),
            event.x_root - self._drag_offset[0], event.y_root - self._drag_offset[1],
            self._screen_bounds(),
        )
        self.geometry(geometry.as_tk())
        self._positioned = True

    def _schedule_animation(self) -> None:
        if not self._closed and not self._reduced_motion and self.visible and self._animation_job is None:
            self._animation_job = self.after(80, self._animate)

    def _animate(self) -> None:
        self._animation_job = None
        if self._closed or not self.visible:
            return
        self._phase = (self._phase + 0.055) % 1.0
        self._draw()
        self._schedule_animation()

    def _cancel_animation(self) -> None:
        if self._animation_job is not None:
            try:
                self.after_cancel(self._animation_job)
            except tk.TclError:
                pass
            self._animation_job = None

    def _draw(self) -> None:
        if self._closed or not hasattr(self, "canvas"):
            return
        try:
            actual_width, actual_height = self.winfo_width(), self.winfo_height()
            preferred_width, preferred_height = self._preferred_size()
            width = preferred_width if actual_width <= 1 else actual_width
            height = preferred_height if actual_height <= 1 else actual_height
            self.canvas.delete("all")
        except tk.TclError:
            return
        get = lambda role: self._colour(role)
        pulse = (math.sin(self._phase * math.tau) + 1.0) / 2.0
        outline = get("accent") if self._reduced_motion else mix_colour(
            get("accent_dim"), get("accent"), 0.35 + pulse * 0.45
        )
        _rounded(
            self.canvas, 2, 2, width - 2, height - 2, 22,
            fill=get("surface"), outline=outline, width=3,
        )
        self.canvas.create_line(24, 39, width - 24, 39, fill=get("border"), width=2)
        self.canvas.create_oval(19, 15, 29, 25, fill=get("accent"), outline=outline, width=2)
        self.canvas.create_text(
            39, 20, anchor="w", text="NEXUS // PERFORMANCE OVERLAY",
            fill=get("text"), font=("Segoe UI", 9, "bold"),
        )
        timestamp = "WAITING FOR TELEMETRY"
        if self._telemetry.captured_at is not None:
            try:
                timestamp = "UPDATED " + datetime.fromtimestamp(
                    self._telemetry.captured_at
                ).strftime("%H:%M:%S")
            except (OSError, OverflowError, ValueError):
                timestamp = "TELEMETRY RECEIVED"
        self.canvas.create_text(
            width - 45, 20, anchor="e", text=timestamp,
            fill=get("muted"), font=("Segoe UI", 8, "bold"),
        )
        self.canvas.create_text(
            width - 22, 19, text="×", fill=get("muted"),
            font=("Segoe UI", 14, "bold"), tags="close",
        )
        left, right, gap = 15, width - 15, 7
        tile_width = (right - left - gap * (len(self._metrics) - 1)) / len(self._metrics)
        for index, metric in enumerate(self._metrics):
            display = metric_display(metric, self._telemetry)
            x1 = left + index * (tile_width + gap)
            x2 = x1 + tile_width
            _rounded(
                self.canvas, x1, 48, x2, height - 13, 15,
                fill=get("surface_alt"), outline=get("border"), width=2,
            )
            self.canvas.create_line(
                x1 + 12, 60, x1 + 12, height - 26,
                fill=get("accent"), width=4, capstyle="round",
            )
            self.canvas.create_text(
                x1 + 23, 63, anchor="w", text=display.label,
                fill=get("muted"), font=("Segoe UI", 8, "bold"),
            )
            self.canvas.create_text(
                x1 + 23, 88, anchor="w", text=display.value,
                fill=get("text"), font=("Segoe UI", 12 if metric == "network" else 17, "bold"),
            )
            self.canvas.create_text(
                x1 + 23, height - 27, anchor="w", text=display.detail,
                fill=get("accent") if metric == "network" else get("muted"),
                font=("Segoe UI", 7, "bold"),
            )
        sweep_width = max(36.0, (width - 40) * 0.16)
        sweep_x = 20 + (0 if self._reduced_motion else self._phase * max(1, width - 40 - sweep_width))
        self.canvas.create_line(
            sweep_x, height - 7, sweep_x + sweep_width, height - 7,
            fill=outline, width=3, capstyle="round",
        )

    def _destroyed(self, event: tk.Event) -> None:
        if event.widget is self:
            self._closed = True
            self._cancel_animation()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_animation()
        callback, self._on_close = self._on_close, None
        try:
            self.destroy()
        except tk.TclError:
            pass
        if callback is not None:
            callback()


__all__ = [
    "DEFAULT_OVERLAY_METRICS", "GamingOverlay", "MetricDisplay",
    "OVERLAY_METRICS", "OverlayTelemetry", "ScreenBounds", "WindowGeometry",
    "clamp_opacity", "fit_geometry", "format_percent", "format_rate",
    "format_temperature", "geometry_string", "metric_display",
    "normalize_overlay_metrics", "parse_geometry", "top_right_geometry",
]
