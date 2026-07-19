"""Standalone Tk feature windows for the NEXUS Hardware Monitor v4.

The module deliberately keeps collection and persistence in the existing
``v4_*`` service modules.  ``FeatureWindowManager`` supplies the small amount
of orchestration needed by a dashboard: it owns at most one window per
feature, exposes launcher callbacks, and accepts live snapshots through
``ingest_snapshot``.

Importing this module never creates a Tk interpreter.  The catalog and data
helpers near the top are therefore safe to exercise in headless tests.
"""

from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # Installed package layout.
    from .alerts import AlertEngine, AlertEvent, AlertRule, metrics_from_snapshot
    from .benchmarks import BenchmarkResult, BenchmarkRunner
    from .diagnostics import DiagnosticResult, DiagnosticState, run_diagnostics
    from .history import HistorySample, HistoryStore, summarize
    from .processes import ProcessInfo, ProcessSnapshot, ProcessTracker
    from .report import build_report, report_json, write_report
    from .sensors import SensorHub, SensorSnapshot, default_sensor_providers
    from .settings import AppSettings, SettingsStore
    from .smart import SmartCapability, SmartDeviceHealth, SmartScanResult, SmartctlRunner
    from .theme import DASHBOARD_METRICS, METRIC_LABELS, SemanticTheme, resolve_theme
except ImportError:  # Staged standalone ``v4_*.py`` files.
    from v4_alerts import AlertEngine, AlertEvent, AlertRule, metrics_from_snapshot
    from v4_benchmarks import BenchmarkResult, BenchmarkRunner
    from v4_diagnostics import DiagnosticResult, DiagnosticState, run_diagnostics
    from v4_history import HistorySample, HistoryStore, summarize
    from v4_processes import ProcessInfo, ProcessSnapshot, ProcessTracker
    from v4_report import build_report, report_json, write_report
    from v4_sensors import SensorHub, SensorSnapshot, default_sensor_providers
    from v4_settings import AppSettings, SettingsStore
    from v4_smart import SmartCapability, SmartDeviceHealth, SmartScanResult, SmartctlRunner
    from v4_theme import DASHBOARD_METRICS, METRIC_LABELS, SemanticTheme, resolve_theme


# -- Pure catalog and presentation helpers ---------------------------------


@dataclass(frozen=True, slots=True)
class FeatureWindowSpec:
    """Stable launcher metadata for one feature window."""

    key: str
    number: str
    title: str
    subtitle: str
    geometry: str

    def __post_init__(self) -> None:
        if not self.key or any(character.isspace() for character in self.key):
            raise ValueError("feature window keys cannot be empty or contain spaces")
        if not self.number.isdecimal():
            raise ValueError("feature window numbers must be decimal text")


FEATURE_WINDOW_CATALOG: tuple[FeatureWindowSpec, ...] = (
    FeatureWindowSpec(
        "processes", "01", "PROCESS EXPLORER",
        "Live, read-only CPU and memory use by application.", "1040x680",
    ),
    FeatureWindowSpec(
        "alerts", "02", "ALERT CENTER",
        "Threshold controls, active warnings, and a recent event log.", "980x700",
    ),
    FeatureWindowSpec(
        "sensors", "03", "SENSORS & DRIVE HEALTH",
        "Optional temperatures, fans, power telemetry, and SMART health.", "1080x720",
    ),
    FeatureWindowSpec(
        "history", "04", "HISTORY VAULT",
        "Explore locally retained telemetry across previous sessions.", "1040x720",
    ),
    FeatureWindowSpec(
        "diagnostics", "05", "NETWORK DIAGNOSTICS",
        "Explicit DNS, reachability, latency, jitter, and loss checks.", "960x700",
    ),
    FeatureWindowSpec(
        "benchmarks", "06", "BENCHMARK CENTER",
        "Short, cancellable CPU, memory, and temporary-file checks.", "940x680",
    ),
    FeatureWindowSpec(
        "reports", "07", "HARDWARE REPORTS",
        "Create a private offline HTML or JSON system report.", "960x690",
    ),
    FeatureWindowSpec(
        "customization", "08", "CUSTOMIZATION STUDIO",
        "Tune appearance, dashboard content, motion, and behaviour.", "980x740",
    ),
)

_WINDOW_ALIASES: Mapping[str, str] = {
    "process": "processes",
    "process_explorer": "processes",
    "alert": "alerts",
    "alert_center": "alerts",
    "smart": "sensors",
    "drive_health": "sensors",
    "network": "diagnostics",
    "network_diagnostics": "diagnostics",
    "benchmark": "benchmarks",
    "report": "reports",
    "settings": "customization",
    "appearance": "customization",
}


def normalize_window_key(value: object) -> str:
    """Normalize control-center aliases to a catalog key."""

    key = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    return _WINDOW_ALIASES.get(key, key)


def feature_window_spec(value: object) -> FeatureWindowSpec:
    key = normalize_window_key(value)
    for spec in FEATURE_WINDOW_CATALOG:
        if spec.key == key:
            return spec
    raise KeyError(f"unknown feature window: {value!r}")


def format_metric(value: object, suffix: str = "", decimals: int = 1) -> str:
    """Format finite numeric telemetry without leaking ``nan`` into the UI."""

    if isinstance(value, bool):
        return "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "N/A"
    if not math.isfinite(number):
        return "N/A"
    try:
        places = int(decimals)
    except (TypeError, ValueError, OverflowError):
        places = 1
    return f"{number:.{max(0, min(places, 6))}f}{suffix}"


def format_data_rate(value: object) -> str:
    try:
        amount = max(0.0, float(value))
    except (TypeError, ValueError, OverflowError):
        return "N/A"
    if not math.isfinite(amount):
        return "N/A"
    units = ("B/s", "KiB/s", "MiB/s", "GiB/s")
    index = 0
    while amount >= 1024.0 and index < len(units) - 1:
        amount /= 1024.0
        index += 1
    return f"{amount:.1f} {units[index]}"


def process_rows(
    snapshot: ProcessSnapshot | None,
    *,
    query: object = "",
    sort_by: str = "cpu",
    descending: bool = True,
    limit: int = 500,
) -> tuple[ProcessInfo, ...]:
    """Filter and deterministically sort a native process snapshot."""

    if snapshot is None:
        return ()
    needle = str(query).strip().casefold()
    rows = [
        item for item in snapshot.processes
        if not needle
        or needle in item.name.casefold()
        or needle in str(item.pid)
        or needle in item.executable.casefold()
    ]
    key_functions: Mapping[str, Callable[[ProcessInfo], object]] = {
        "pid": lambda item: item.pid,
        "name": lambda item: item.name.casefold(),
        "cpu": lambda item: item.cpu_percent if item.cpu_percent is not None else -1.0,
        "memory": lambda item: item.memory_mib if item.memory_mib is not None else -1.0,
        "threads": lambda item: item.threads if item.threads is not None else -1,
    }
    selected = key_functions.get(str(sort_by).casefold(), key_functions["cpu"])
    rows.sort(key=lambda item: (selected(item), item.name.casefold(), item.pid), reverse=bool(descending))
    try:
        row_limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        row_limit = 500
    return tuple(rows[:max(1, min(row_limit, 5_000))])


HISTORY_METRICS: Mapping[str, tuple[str, str, float | None, float | None]] = {
    "cpu": ("cpu_percent", "%", 0.0, 100.0),
    "memory": ("memory_percent", "%", 0.0, 100.0),
    "storage": ("storage_percent", "%", 0.0, 100.0),
    "temperature": ("temperature_c", "°C", None, None),
    "download": ("network_down_bps", "B/s", 0.0, None),
    "upload": ("network_up_bps", "B/s", 0.0, None),
}


def history_series(
    samples: Iterable[HistorySample], metric: object
) -> tuple[tuple[float, float], ...]:
    """Return finite, chronological ``(timestamp, value)`` pairs."""

    key = str(metric).strip().casefold()
    if key not in HISTORY_METRICS:
        raise ValueError(f"unknown history metric: {metric!r}")
    field = HISTORY_METRICS[key][0]
    values: list[tuple[float, float]] = []
    for sample in samples:
        try:
            timestamp = float(sample.timestamp)
            value = float(getattr(sample, field))
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(timestamp) and math.isfinite(value):
            values.append((timestamp, value))
    values.sort(key=lambda item: item[0])
    return tuple(values)


def scale_history_points(
    samples: Iterable[HistorySample],
    metric: object,
    width: object,
    height: object,
    *,
    padding: int = 28,
) -> tuple[tuple[float, float], ...]:
    """Scale one history series into canvas coordinates."""

    series = history_series(samples, metric)
    if not series:
        return ()
    canvas_width = max(2 * padding + 1, float(width))
    canvas_height = max(2 * padding + 1, float(height))
    times = [item[0] for item in series]
    values = [item[1] for item in series]
    configured_low, configured_high = HISTORY_METRICS[str(metric).strip().casefold()][2:]
    low = min(values) if configured_low is None else configured_low
    high = max(values) if configured_high is None else configured_high
    if high <= low:
        high = low + 1.0
    first, last = times[0], times[-1]
    span = max(last - first, 1.0)
    usable_width = canvas_width - 2 * padding
    usable_height = canvas_height - 2 * padding
    return tuple(
        (
            padding + (timestamp - first) / span * usable_width,
            canvas_height - padding - (value - low) / (high - low) * usable_height,
        )
        for timestamp, value in series
    )


def alert_rules_from_settings(settings: AppSettings) -> tuple[AlertRule, ...]:
    """Create the four alert rules represented by ``AppSettings``."""

    return (
        AlertRule("cpu", "CPU load", settings.cpu_alert_percent, hold_seconds=4.0),
        AlertRule("memory", "Memory use", settings.memory_alert_percent, hold_seconds=5.0),
        AlertRule(
            "storage", "Storage use", settings.storage_alert_percent,
            hold_seconds=15.0, cooldown_seconds=300.0,
        ),
        AlertRule(
            "temperature", "Temperature", settings.temperature_alert_c,
            unit="°C", hold_seconds=3.0,
        ),
    )


def alert_metrics_from_snapshot(
    snapshot: object, temperature_c: float | None = None,
) -> dict[str, float | None]:
    """Use the fullest detected volume, not only the operating-system drive."""
    metrics = metrics_from_snapshot(snapshot, temperature_c)
    drive_values: list[float] = []
    for drive in getattr(snapshot, "drives", ()) or ():
        try:
            value = float(getattr(drive, "used_percent"))
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            drive_values.append(value)
    if drive_values:
        metrics["storage"] = max(drive_values)
    return metrics


def settings_with_updates(settings: AppSettings, updates: Mapping[str, object]) -> AppSettings:
    values = settings.as_dict()
    values.update(updates)
    return AppSettings.from_mapping(values)


def report_default_filename(format: object = "html", timestamp: float | None = None) -> str:
    selected = str(format).strip().casefold()
    suffix = "json" if selected == "json" else "html"
    moment = datetime.fromtimestamp(
        time.time() if timestamp is None else float(timestamp), timezone.utc
    )
    return f"nexus-hardware-report-{moment:%Y%m%d-%H%M%S}.{suffix}"


def diagnostic_summary_lines(result: DiagnosticResult) -> tuple[str, ...]:
    """Create compact result lines shared by the GUI and headless tests."""

    method = result.method.value.upper() if result.method is not None else "NONE"
    lines = [
        f"STATE  {result.state.value.upper()}",
        f"TARGET  {result.target or 'N/A'}",
        f"DNS  {format_metric(result.dns_ms, ' ms', 2)}",
        f"METHOD  {method}",
        f"RECEIVED  {result.received}/{result.sent}",
        f"AVERAGE  {format_metric(result.average_ms, ' ms', 2)}",
        f"JITTER  {format_metric(result.jitter_ms, ' ms', 2)}",
    ]
    if result.packet_loss_percent is not None:
        lines.append(f"PACKET LOSS  {format_metric(result.packet_loss_percent, '%', 1)}")
    elif result.failure_percent is not None:
        lines.append(f"TCP FAILURES  {format_metric(result.failure_percent, '%', 1)}")
    if result.detail:
        lines.append(f"NOTE  {result.detail}")
    return tuple(lines)


# -- Tk building blocks -----------------------------------------------------


def _rounded_rectangle(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    radius: float = 16,
    **options: object,
) -> int:
    radius = max(2.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = (
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    )
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


class RoundedCard(tk.Canvas):
    """Canvas-backed rounded container with a normal Tk frame inside."""

    def __init__(
        self,
        parent: tk.Misc,
        theme: SemanticTheme,
        *,
        height: int = 120,
        padding: int = 14,
        surface: str | None = None,
    ) -> None:
        super().__init__(
            parent, bg=theme.background, height=height, highlightthickness=0,
            bd=0, relief="flat",
        )
        self.theme = theme
        self.surface = surface or theme.surface
        self.padding = padding
        self.content = tk.Frame(self, bg=self.surface)
        self._content_id = self.create_window(padding, padding, anchor="nw", window=self.content)
        self.bind("<Configure>", self._draw, add="+")

    def _draw(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        width, height = self.winfo_width(), self.winfo_height()
        if width < 3 or height < 3:
            return
        self.delete("card_surface")
        _rounded_rectangle(
            self, 2, 2, width - 2, height - 2, radius=18,
            fill=self.surface, outline=self.theme.border, width=2,
            tags="card_surface",
        )
        self.tag_lower("card_surface")
        self.coords(self._content_id, self.padding, self.padding)
        self.itemconfigure(
            self._content_id,
            width=max(1, width - 2 * self.padding),
            height=max(1, height - 2 * self.padding),
        )


class NexusButton(tk.Canvas):
    """Small keyboard-accessible rounded Canvas button."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        theme: SemanticTheme,
        *,
        primary: bool = False,
        width: int | None = None,
    ) -> None:
        self.label = text
        self.command = command
        self.theme = theme
        self.primary = primary
        self.enabled = True
        button_width = width or max(86, len(text) * 8 + 30)
        super().__init__(
            parent, width=button_width, height=36, bg=parent.cget("bg"),
            highlightthickness=0, bd=0, takefocus=True, cursor="hand2",
        )
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Enter>", lambda _event: self._draw(True))
        self.bind("<Leave>", lambda _event: self._draw(False))
        self.bind("<Button-1>", self._invoke)
        self.bind("<Return>", self._invoke)
        self.bind("<space>", self._invoke)
        self._draw()

    def _draw(self, hover: bool = False) -> None:
        self.delete("all")
        if not self.enabled:
            fill, foreground, outline = self.theme.surface_alt, self.theme.muted, self.theme.border
        elif self.primary:
            fill = self.theme.accent_hover if hover else self.theme.accent
            foreground, outline = self.theme.text, self.theme.accent
        else:
            fill = self.theme.surface_alt if not hover else self.theme.border
            foreground, outline = self.theme.text, self.theme.border_strong
        _rounded_rectangle(
            self, 2, 2, max(3, self.winfo_width() - 2), max(3, self.winfo_height() - 2),
            radius=11, fill=fill, outline=outline, width=2,
        )
        self.create_text(
            self.winfo_width() / 2, self.winfo_height() / 2,
            text=self.label, fill=foreground, font=("Segoe UI", 9, "bold"),
        )

    def _invoke(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if self.enabled:
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self._draw()


class HistoryCanvas(tk.Canvas):
    """Simple dependency-free time-series graph."""

    def __init__(self, parent: tk.Misc, theme: SemanticTheme) -> None:
        super().__init__(
            parent, bg=theme.surface, highlightthickness=0, bd=0,
            height=330,
        )
        self.theme = theme
        self.samples: tuple[HistorySample, ...] = ()
        self.metric = "cpu"
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_data(self, samples: Iterable[HistorySample], metric: str) -> None:
        self.samples = tuple(samples)
        self.metric = metric
        self.redraw()

    def redraw(self) -> None:
        width, height = self.winfo_width(), self.winfo_height()
        if width < 80 or height < 80:
            return
        self.delete("all")
        padding = 34
        for index in range(5):
            y = padding + (height - 2 * padding) * index / 4
            self.create_line(
                padding, y, width - padding, y,
                fill=self.theme.grid, width=2 if index in {0, 4} else 1,
            )
        points = scale_history_points(
            self.samples, self.metric, width, height, padding=padding
        )
        if not points:
            self.create_text(
                width / 2, height / 2, text="NO HISTORY IN THIS RANGE",
                fill=self.theme.muted, font=("Segoe UI", 11, "bold"),
            )
            return
        flattened = [coordinate for point in points for coordinate in point]
        if len(points) > 1:
            polygon = [points[0][0], height - padding, *flattened, points[-1][0], height - padding]
            self.create_polygon(polygon, fill=self.theme.graph_fill, outline="")
            self.create_line(*flattened, fill=self.theme.accent, width=3, smooth=True)
        else:
            x, y = points[0]
            self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=self.theme.accent, outline="")
        series = history_series(self.samples, self.metric)
        values = [value for _timestamp, value in series]
        unit = HISTORY_METRICS[self.metric][1]
        if values:
            label = (
                f"MIN {format_metric(min(values), unit, 1)}    "
                f"MAX {format_metric(max(values), unit, 1)}"
            )
            self.create_text(
                padding, 14, anchor="w", text=label,
                fill=self.theme.muted, font=("Cascadia Mono", 9, "bold"),
            )


def _configure_ttk(window: tk.Misc, theme: SemanticTheme) -> None:
    style = ttk.Style(window)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        "Nexus.Treeview", background=theme.surface_alt, fieldbackground=theme.surface_alt,
        foreground=theme.text, bordercolor=theme.border, borderwidth=0,
        rowheight=29, font=("Segoe UI", 9),
    )
    style.configure(
        "Nexus.Treeview.Heading", background=theme.panel, foreground=theme.text,
        bordercolor=theme.border, borderwidth=2, relief="flat",
        font=("Segoe UI", 9, "bold"),
    )
    style.map(
        "Nexus.Treeview", background=[("selected", theme.accent_dim)],
        foreground=[("selected", theme.text)],
    )
    style.configure(
        "Nexus.Horizontal.TProgressbar", background=theme.accent,
        troughcolor=theme.track, bordercolor=theme.track,
    )
    style.configure(
        "Nexus.TCombobox", fieldbackground=theme.surface_alt,
        background=theme.surface_alt, foreground=theme.text,
        arrowcolor=theme.text,
    )
    style.configure(
        "Nexus.TNotebook", background=theme.background, borderwidth=0,
        tabmargins=(0, 0, 0, 4),
    )
    style.configure(
        "Nexus.TNotebook.Tab", background=theme.surface_alt,
        foreground=theme.muted, borderwidth=0, padding=(15, 8),
        font=("Segoe UI", 9, "bold"),
    )
    style.map(
        "Nexus.TNotebook.Tab",
        background=[("selected", theme.accent_dim), ("active", theme.border)],
        foreground=[("selected", theme.text), ("active", theme.text)],
    )
    style.configure(
        "Nexus.Vertical.TScrollbar", background=theme.surface_alt,
        troughcolor=theme.background, bordercolor=theme.background,
        arrowcolor=theme.text, relief="flat", borderwidth=0,
    )


@dataclass(slots=True)
class FeatureServices:
    """Optional service objects and dashboard callbacks used by the manager."""

    get_snapshot: Callable[[], object | None] | None = None
    get_hardware: Callable[[], object | None] | None = None
    get_network: Callable[[], object | None] | None = None
    get_sensor_snapshot: Callable[[], SensorSnapshot | None] | None = None
    get_drive_health: Callable[[], Iterable[SmartDeviceHealth]] | None = None
    get_benchmark_results: Callable[[], Iterable[BenchmarkResult]] | None = None
    history_store: HistoryStore | None = None
    sensor_hub: SensorHub | None = None
    smart_runner: SmartctlRunner | None = None
    settings_store: SettingsStore | None = None
    on_settings_changed: Callable[[AppSettings], None] | None = None
    on_status_changed: Callable[[str, Mapping[str, object]], None] | None = None


class FeatureWindowController:
    """Common window chrome and a Tk-safe background result queue."""

    def __init__(self, manager: "FeatureWindowManager", spec: FeatureWindowSpec) -> None:
        self.manager = manager
        self.spec = spec
        self.theme = manager.theme
        self.window = tk.Toplevel(manager.parent)
        self.window.withdraw()
        self.window.title(f"NEXUS // {spec.title.title()}")
        self.window.geometry(spec.geometry)
        self.window.minsize(760, 560)
        self.window.configure(bg=self.theme.background)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._closed = False
        self._lifetime_cancel = threading.Event()
        self._messages: queue.Queue[tuple[Callable[..., None], tuple[object, ...]]] = queue.Queue()
        self._worker_threads: list[threading.Thread] = []
        self._poll_job: str | None = None
        _configure_ttk(self.window, self.theme)
        self._build_chrome()
        self.build()
        self._poll_job = self.window.after(60, self._poll_messages)
        self._place_near_parent()
        self.window.deiconify()
        self.window.lift()

    @property
    def is_open(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def _build_chrome(self) -> None:
        header = tk.Frame(self.window, bg=self.theme.panel, height=86)
        header.pack(fill="x")
        header.pack_propagate(False)
        number = tk.Label(
            header, text=self.spec.number, bg=self.theme.panel, fg=self.theme.accent,
            font=("Cascadia Mono", 22, "bold"),
        )
        number.pack(side="left", padx=(24, 14))
        titles = tk.Frame(header, bg=self.theme.panel)
        titles.pack(side="left", fill="y", pady=15)
        tk.Label(
            titles, text=self.spec.title, bg=self.theme.panel, fg=self.theme.text,
            font=("Segoe UI", 15, "bold"), anchor="w",
        ).pack(anchor="w")
        tk.Label(
            titles, text=self.spec.subtitle, bg=self.theme.panel, fg=self.theme.muted,
            font=("Segoe UI", 9), anchor="w",
        ).pack(anchor="w", pady=(2, 0))
        NexusButton(header, "CLOSE", self.close, self.theme, width=82).pack(
            side="right", padx=22, pady=24
        )
        tk.Frame(self.window, bg=self.theme.accent, height=3).pack(fill="x")
        self.body = tk.Frame(self.window, bg=self.theme.background)
        self.body.pack(fill="both", expand=True, padx=18, pady=16)
        footer = tk.Frame(self.window, bg=self.theme.panel, height=34)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        self.status_label = tk.Label(
            footer, text="READY", bg=self.theme.panel, fg=self.theme.muted,
            font=("Cascadia Mono", 8, "bold"), anchor="w",
        )
        self.status_label.pack(fill="both", padx=18)

    def build(self) -> None:
        raise NotImplementedError

    def _place_near_parent(self) -> None:
        try:
            self.window.update_idletasks()
            parent_x = self.manager.parent.winfo_rootx()
            parent_y = self.manager.parent.winfo_rooty()
            x = max(0, parent_x + 38)
            y = max(0, parent_y + 38)
            size = self.spec.geometry.split("+", 1)[0]
            self.window.geometry(f"{size}+{x}+{y}")
        except (AttributeError, tk.TclError, ValueError):
            return

    def set_status(self, text: str, *, attention: bool = False) -> None:
        if not self.is_open:
            return
        self.status_label.configure(
            text=str(text).upper(),
            fg=self.theme.accent if attention else self.theme.muted,
        )

    def post(self, callback: Callable[..., None], *args: object) -> None:
        if not self._closed:
            self._messages.put((callback, args))

    def run_background(
        self,
        operation: Callable[[], object],
        complete: Callable[[object], None],
        failed: Callable[[BaseException], None] | None = None,
    ) -> threading.Thread:
        """Run an operation without making any Tk call from its worker."""

        def work() -> None:
            try:
                result = operation()
            except BaseException as exc:  # Worker boundary must preserve the Tk loop.
                self.post(failed or self._background_failed, exc)
            else:
                self.post(complete, result)

        thread = threading.Thread(
            target=work, name=f"nexus-{self.spec.key}-worker", daemon=True
        )
        self._worker_threads.append(thread)
        thread.start()
        return thread

    def _background_failed(self, error: BaseException) -> None:
        self.set_status(f"ERROR // {error}", attention=True)

    def _poll_messages(self) -> None:
        if self._closed:
            return
        try:
            while True:
                callback, args = self._messages.get_nowait()
                try:
                    callback(*args)
                except Exception as exc:
                    if not self._closed:
                        self._background_failed(exc)
        except queue.Empty:
            pass
        if not self._closed:
            self._poll_job = self.window.after(60, self._poll_messages)

    def on_settings_changed(self, _settings: AppSettings) -> None:
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._lifetime_cancel.set()
        if self._poll_job is not None:
            try:
                self.window.after_cancel(self._poll_job)
            except tk.TclError:
                pass
        self.manager._window_closed(self.spec.key, self)
        for thread in self._worker_threads:
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(0.05)
        try:
            self.window.destroy()
        except tk.TclError:
            pass


def _tree(
    parent: tk.Misc,
    columns: Sequence[tuple[str, str, int]],
    *,
    height: int = 12,
) -> tuple[ttk.Treeview, ttk.Scrollbar]:
    names = tuple(item[0] for item in columns)
    tree = ttk.Treeview(
        parent, columns=names, show="headings", style="Nexus.Treeview", height=height
    )
    for name, heading, width in columns:
        tree.heading(name, text=heading)
        tree.column(name, width=width, minwidth=50, anchor="w", stretch=True)
    scrollbar = ttk.Scrollbar(
        parent, orient="vertical", command=tree.yview,
        style="Nexus.Vertical.TScrollbar",
    )
    tree.configure(yscrollcommand=scrollbar.set)
    return tree, scrollbar


def _label(
    parent: tk.Misc,
    text: str,
    theme: SemanticTheme,
    *,
    muted: bool = False,
    bold: bool = False,
    size: int = 9,
    **options: object,
) -> tk.Label:
    return tk.Label(
        parent, text=text, bg=parent.cget("bg"),
        fg=theme.muted if muted else theme.text,
        font=("Segoe UI", size, "bold" if bold else "normal"),
        **options,
    )


# -- Feature controllers ----------------------------------------------------


class ProcessExplorerWindow(FeatureWindowController):
    def build(self) -> None:
        self.tracker = ProcessTracker()
        self.snapshot: ProcessSnapshot | None = self.manager.process_snapshot
        self._dashboard_feed = hasattr(self.manager.dashboard, "latest_processes")
        self.sort_by = "cpu"
        self.descending = True
        self._sampling = False
        self._refresh_job: str | None = None

        controls = tk.Frame(self.body, bg=self.theme.background)
        controls.pack(fill="x", pady=(0, 12))
        _label(controls, "FILTER", self.theme, muted=True, bold=True).pack(side="left")
        self.query = tk.StringVar()
        search = tk.Entry(
            controls, textvariable=self.query, bg=self.theme.surface_alt,
            fg=self.theme.text, insertbackground=self.theme.text,
            highlightbackground=self.theme.border, highlightcolor=self.theme.accent,
            highlightthickness=2, relief="flat", font=("Segoe UI", 10), width=28,
        )
        search.pack(side="left", padx=(9, 14), ipady=6)
        search.bind("<KeyRelease>", lambda _event: self._render())
        self.refresh_button = NexusButton(
            controls, "REFRESH", self.refresh, self.theme, primary=True
        )
        self.refresh_button.pack(side="right")
        self.auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            controls, text="AUTO 2s", variable=self.auto_var,
            command=self._schedule_refresh, bg=self.theme.background,
            fg=self.theme.text, selectcolor=self.theme.surface_alt,
            activebackground=self.theme.background, activeforeground=self.theme.text,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="right", padx=14)

        table_card = RoundedCard(self.body, self.theme, height=430)
        table_card.pack(fill="both", expand=True)
        table = table_card.content
        columns = (
            ("pid", "PID", 75), ("name", "PROCESS", 230),
            ("cpu", "CPU", 85), ("memory", "MEMORY", 105),
            ("percent", "RAM", 80), ("threads", "THREADS", 80),
        )
        self.tree, scrollbar = _tree(table, columns, height=14)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for name, _heading, _width in columns:
            self.tree.heading(name, command=lambda column=name: self._change_sort(column))
        self.tree.bind("<<TreeviewSelect>>", self._select_process)

        detail_card = RoundedCard(self.body, self.theme, height=80)
        detail_card.pack(fill="x", pady=(12, 0))
        self.detail = _label(
            detail_card.content, "Select a process to inspect its executable path.",
            self.theme, muted=True, anchor="w", justify="left", wraplength=900,
        )
        self.detail.pack(fill="both", expand=True)
        self._render()
        self.refresh()

    def _change_sort(self, column: str) -> None:
        aliases = {"percent": "memory"}
        selected = aliases.get(column, column)
        if self.sort_by == selected:
            self.descending = not self.descending
        else:
            self.sort_by = selected
            self.descending = selected != "name"
        self._render()

    def refresh(self) -> None:
        if self._sampling or not self.is_open:
            return
        if self._dashboard_feed:
            dashboard_snapshot = getattr(self.manager.dashboard, "latest_processes", None)
            if isinstance(dashboard_snapshot, ProcessSnapshot):
                self.snapshot = dashboard_snapshot
                self.manager.process_snapshot = dashboard_snapshot
                self._render()
                self.set_status(
                    f"{len(dashboard_snapshot.processes)} PROCESSES // LIVE DASHBOARD FEED"
                )
            else:
                self.set_status("WAITING FOR THE DASHBOARD PROCESS FEED")
            self._schedule_refresh()
            return
        self._sampling = True
        self.refresh_button.set_enabled(False)
        self.set_status("SAMPLING NATIVE PROCESS DATA")
        self.run_background(self.tracker.sample, self._sample_complete, self._sample_failed)

    def _sample_complete(self, result: object) -> None:
        self._sampling = False
        self.refresh_button.set_enabled(True)
        if not isinstance(result, ProcessSnapshot):
            self._sample_failed(TypeError("process service returned an unexpected result"))
            return
        self.snapshot = result
        self._render()
        self.set_status(
            f"{len(result.processes)} PROCESSES // {result.inaccessible_count} PARTIALLY INACCESSIBLE"
        )
        self._schedule_refresh()

    def _sample_failed(self, error: BaseException) -> None:
        self._sampling = False
        self.refresh_button.set_enabled(True)
        self.set_status(f"PROCESS SAMPLE FAILED // {error}", attention=True)
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self._refresh_job is not None:
            try:
                self.window.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
            self._refresh_job = None
        if self.is_open and self.auto_var.get():
            self._refresh_job = self.window.after(2_000, self.refresh)

    def _render(self) -> None:
        selection = self.tree.selection()
        selected_pid = selection[0] if selection else None
        self.tree.delete(*self.tree.get_children())
        rows = process_rows(
            self.snapshot, query=self.query.get(), sort_by=self.sort_by,
            descending=self.descending,
        )
        for process in rows:
            identifier = str(process.pid)
            self.tree.insert(
                "", "end", iid=identifier,
                values=(
                    process.pid, process.name,
                    format_metric(process.cpu_percent, "%", 1),
                    format_metric(process.memory_mib, " MiB", 1),
                    format_metric(process.memory_percent, "%", 2),
                    process.threads if process.threads is not None else "N/A",
                ),
            )
        if selected_pid and self.tree.exists(selected_pid):
            self.tree.selection_set(selected_pid)

    def _select_process(self, _event: tk.Event[tk.Misc]) -> None:
        selected = self.tree.selection()
        if not selected or self.snapshot is None:
            return
        pid = int(selected[0])
        process = next((item for item in self.snapshot.processes if item.pid == pid), None)
        if process is None:
            return
        executable = process.executable or "Executable path unavailable (permission or system process)."
        self.detail.configure(
            text=f"{process.name}  //  PID {process.pid}\n{executable}", fg=self.theme.text
        )

    def close(self) -> None:
        if getattr(self, "_refresh_job", None) is not None:
            try:
                self.window.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
        super().close()


class AlertCenterWindow(FeatureWindowController):
    _FIELDS = (
        ("cpu_alert_percent", "CPU LOAD", "%"),
        ("memory_alert_percent", "MEMORY USE", "%"),
        ("storage_alert_percent", "STORAGE USE", "%"),
        ("temperature_alert_c", "TEMPERATURE", "°C"),
    )

    def build(self) -> None:
        settings = self.manager.settings
        self.enabled_var = tk.BooleanVar(value=settings.alerts_enabled)
        self.threshold_vars: dict[str, tk.StringVar] = {}

        settings_card = RoundedCard(self.body, self.theme, height=164)
        settings_card.pack(fill="x")
        top = tk.Frame(settings_card.content, bg=self.theme.surface)
        top.pack(fill="x")
        _label(top, "ALERT THRESHOLDS", self.theme, bold=True, size=11).pack(side="left")
        tk.Checkbutton(
            top, text="ALERTS ENABLED", variable=self.enabled_var,
            bg=self.theme.surface, fg=self.theme.text,
            selectcolor=self.theme.surface_alt, activebackground=self.theme.surface,
            activeforeground=self.theme.text, font=("Segoe UI", 9, "bold"),
        ).pack(side="right")
        fields = tk.Frame(settings_card.content, bg=self.theme.surface)
        fields.pack(fill="x", pady=(12, 0))
        for column, (field, label, unit) in enumerate(self._FIELDS):
            cell = tk.Frame(fields, bg=self.theme.surface)
            cell.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 7, 7))
            fields.columnconfigure(column, weight=1)
            _label(cell, label, self.theme, muted=True, bold=True, size=8).pack(anchor="w")
            row = tk.Frame(cell, bg=self.theme.surface)
            row.pack(fill="x", pady=(5, 0))
            variable = tk.StringVar(value=f"{getattr(settings, field):g}")
            self.threshold_vars[field] = variable
            tk.Entry(
                row, textvariable=variable, width=8, bg=self.theme.surface_alt,
                fg=self.theme.text, insertbackground=self.theme.text,
                highlightthickness=2, highlightbackground=self.theme.border,
                highlightcolor=self.theme.accent, relief="flat", font=("Cascadia Mono", 10),
            ).pack(side="left", fill="x", expand=True, ipady=5)
            _label(row, unit, self.theme, muted=True, bold=True).pack(side="left", padx=(6, 0))
        action_row = tk.Frame(settings_card.content, bg=self.theme.surface)
        action_row.pack(fill="x", pady=(11, 0))
        NexusButton(
            action_row, "SAVE THRESHOLDS", self._save, self.theme, primary=True, width=150
        ).pack(side="left")
        NexusButton(
            action_row, "RESOLVE ACTIVE", self._resolve, self.theme, width=132
        ).pack(side="left", padx=8)
        self.active_label = _label(
            action_row, "NO ACTIVE WARNINGS", self.theme, muted=True, bold=True
        )
        self.active_label.pack(side="right")

        metrics_card = RoundedCard(self.body, self.theme, height=82)
        metrics_card.pack(fill="x", pady=(12, 0))
        self.metric_labels: dict[str, tk.Label] = {}
        for column, key in enumerate(("cpu", "memory", "storage", "temperature")):
            frame = tk.Frame(metrics_card.content, bg=self.theme.surface)
            frame.grid(row=0, column=column, sticky="nsew")
            metrics_card.content.columnconfigure(column, weight=1)
            _label(frame, key.upper(), self.theme, muted=True, bold=True, size=8).pack()
            value = _label(frame, "N/A", self.theme, bold=True, size=12)
            value.pack(pady=(4, 0))
            self.metric_labels[key] = value

        event_card = RoundedCard(self.body, self.theme, height=320)
        event_card.pack(fill="both", expand=True, pady=(12, 0))
        self.events, scrollbar = _tree(
            event_card.content,
            (("time", "TIME", 145), ("state", "STATE", 90),
             ("metric", "METRIC", 130), ("message", "EVENT", 430)),
            height=10,
        )
        self.events.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._refresh_job: str | None = None
        self._tick()

    def _save(self) -> None:
        updates: dict[str, object] = {"alerts_enabled": self.enabled_var.get()}
        try:
            for field, variable in self.threshold_vars.items():
                updates[field] = float(variable.get().strip())
        except ValueError:
            self.set_status("THRESHOLDS MUST BE NUMBERS", attention=True)
            return
        settings = settings_with_updates(self.manager.settings, updates)
        self.manager.update_settings(settings)
        for field, variable in self.threshold_vars.items():
            variable.set(f"{getattr(settings, field):g}")
        self.set_status("ALERT SETTINGS SAVED")

    def _resolve(self) -> None:
        events = self.manager.resolve_alerts()
        self._render_events()
        self.set_status(f"{len(events)} ACTIVE ALERTS RESOLVED")

    def _tick(self) -> None:
        if not self.is_open:
            return
        snapshot = self.manager.current_snapshot()
        metrics = alert_metrics_from_snapshot(
            snapshot, self.manager.current_temperature()
        ) if snapshot else {}
        for key, label in self.metric_labels.items():
            suffix = "°C" if key == "temperature" else "%"
            label.configure(text=format_metric(metrics.get(key), suffix, 1))
        active = self.manager.alert_engine.active_keys if self.manager.settings.alerts_enabled else ()
        self.active_label.configure(
            text=("ACTIVE // " + ", ".join(key.upper() for key in active)) if active else "NO ACTIVE WARNINGS",
            fg=self.theme.accent if active else self.theme.muted,
        )
        self._render_events()
        self._refresh_job = self.window.after(1_000, self._tick)

    def _render_events(self) -> None:
        self.events.delete(*self.events.get_children())
        for index, event in enumerate(self.manager.alert_events):
            timestamp = datetime.fromtimestamp(event.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            self.events.insert(
                "", "end", iid=f"event-{index}",
                values=(timestamp, event.kind.upper(), event.label, event.message),
            )

    def on_settings_changed(self, settings: AppSettings) -> None:
        self.enabled_var.set(settings.alerts_enabled)

    def close(self) -> None:
        if getattr(self, "_refresh_job", None) is not None:
            try:
                self.window.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
        super().close()


class SensorHealthWindow(FeatureWindowController):
    def build(self) -> None:
        controls = tk.Frame(self.body, bg=self.theme.background)
        controls.pack(fill="x", pady=(0, 12))
        _label(
            controls,
            "Optional providers fail closed; drive checks never wake sleeping disks.",
            self.theme, muted=True,
        ).pack(side="left")
        self.refresh_button = NexusButton(
            controls, "REFRESH HEALTH", self.refresh, self.theme, primary=True, width=145
        )
        self.refresh_button.pack(side="right")

        notebook = ttk.Notebook(self.body, style="Nexus.TNotebook")
        notebook.pack(fill="both", expand=True)
        sensor_tab = tk.Frame(notebook, bg=self.theme.background)
        smart_tab = tk.Frame(notebook, bg=self.theme.background)
        notebook.add(sensor_tab, text="  LIVE SENSORS  ")
        notebook.add(smart_tab, text="  DRIVE HEALTH  ")

        sensor_card = RoundedCard(sensor_tab, self.theme, height=455)
        sensor_card.pack(fill="both", expand=True)
        self.sensor_tree, sensor_scroll = _tree(
            sensor_card.content,
            (("hardware", "HARDWARE", 210), ("sensor", "SENSOR", 210),
             ("kind", "KIND", 110), ("value", "VALUE", 110),
             ("source", "SOURCE", 160)),
            height=14,
        )
        self.sensor_tree.pack(side="left", fill="both", expand=True)
        sensor_scroll.pack(side="right", fill="y")
        self.provider_label = _label(
            sensor_tab, "Providers not sampled yet.", self.theme,
            muted=True, anchor="w", justify="left", wraplength=980,
        )
        self.provider_label.pack(fill="x", pady=(9, 0))

        smart_card = RoundedCard(smart_tab, self.theme, height=455)
        smart_card.pack(fill="both", expand=True)
        self.smart_tree, smart_scroll = _tree(
            smart_card.content,
            (("model", "DRIVE", 240), ("capability", "ACCESS", 110),
             ("health", "HEALTH", 100), ("temperature", "TEMP", 85),
             ("hours", "POWER-ON", 100), ("wear", "USED", 80),
             ("detail", "DETAIL", 240)),
            height=14,
        )
        self.smart_tree.pack(side="left", fill="both", expand=True)
        smart_scroll.pack(side="right", fill="y")
        self.smart_label = _label(
            smart_tab, "smartctl has not been scanned.", self.theme,
            muted=True, anchor="w", justify="left", wraplength=980,
        )
        self.smart_label.pack(fill="x", pady=(9, 0))
        self._refreshing = False
        self.refresh()

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.refresh_button.set_enabled(False)
        self.set_status("READING OPTIONAL SENSOR AND SMART PROVIDERS")
        self.run_background(self.manager.collect_health, self._complete, self._failed)

    def _complete(self, result: object) -> None:
        self._refreshing = False
        self.refresh_button.set_enabled(True)
        if not isinstance(result, tuple) or len(result) != 3:
            self._failed(TypeError("health service returned an unexpected result"))
            return
        sensors, scan, health = result
        if not isinstance(sensors, SensorSnapshot) or not isinstance(scan, SmartScanResult):
            self._failed(TypeError("health service returned an unexpected result"))
            return
        health = tuple(health)
        self.manager.sensor_snapshot = sensors
        self.manager.drive_health = health
        if hasattr(self.manager.dashboard, "latest_sensor_snapshot"):
            self.manager.dashboard.latest_sensor_snapshot = sensors
        if hasattr(self.manager.dashboard, "latest_smart_health"):
            self.manager.dashboard.latest_smart_health = health
        self._render_sensors(sensors)
        self._render_smart(scan, health)
        warnings = sum(
            1 for item in health
            if getattr(item.health, "value", str(item.health)) in {"warning", "failed"}
        )
        self.set_status(
            f"{len(sensors.readings)} SENSOR VALUES // {len(health)} DRIVES"
            + (f" // {warnings} NEED ATTENTION" if warnings else ""),
            attention=bool(warnings),
        )
        self.manager._notify_status("sensors")

    def _failed(self, error: BaseException) -> None:
        self._refreshing = False
        self.refresh_button.set_enabled(True)
        self.set_status(f"HEALTH REFRESH FAILED // {error}", attention=True)

    def _render_sensors(self, snapshot: SensorSnapshot) -> None:
        self.sensor_tree.delete(*self.sensor_tree.get_children())
        for index, reading in enumerate(snapshot.readings):
            flags = "  !" if reading.alarm or reading.fault else ""
            self.sensor_tree.insert(
                "", "end", iid=f"sensor-{index}",
                values=(
                    reading.hardware, reading.label, reading.kind.value.upper(),
                    f"{format_metric(reading.value, '', 1)} {reading.unit}{flags}".strip(),
                    reading.source,
                ),
            )
        lines = []
        for provider in snapshot.providers:
            line = f"{provider.provider}: {provider.state.value}"
            if provider.detail:
                line += f" — {provider.detail}"
            lines.append(line)
        self.provider_label.configure(text="   |   ".join(lines) or "No sensor providers configured.")

    def _render_smart(
        self, scan: SmartScanResult, health: Sequence[SmartDeviceHealth]
    ) -> None:
        self.smart_tree.delete(*self.smart_tree.get_children())
        for index, item in enumerate(health):
            model = item.model or item.device.info_name or "Drive"
            detail = item.detail or "; ".join(issue.summary for issue in item.issues[:2])
            self.smart_tree.insert(
                "", "end", iid=f"drive-{index}",
                values=(
                    model, item.capability.value.upper(), item.health.value.upper(),
                    format_metric(item.temperature_c, "°C", 0),
                    (f"{item.power_on_hours:,} h" if item.power_on_hours is not None else "N/A"),
                    format_metric(item.percentage_used, "%", 0), detail,
                ),
            )
        self.smart_label.configure(
            text=(
                f"smartctl: {scan.capability.value}. {scan.detail}".strip()
                if scan.detail or not health
                else f"smartctl: {scan.capability.value}; {len(health)} drive(s) read."
            )
        )


class HistoryViewerWindow(FeatureWindowController):
    _RANGES: Mapping[str, float] = {
        "LAST HOUR": 3_600,
        "LAST 6 HOURS": 21_600,
        "LAST 24 HOURS": 86_400,
        "LAST 7 DAYS": 604_800,
        "LAST 30 DAYS": 2_592_000,
    }

    def build(self) -> None:
        controls = tk.Frame(self.body, bg=self.theme.background)
        controls.pack(fill="x", pady=(0, 12))
        _label(controls, "RANGE", self.theme, muted=True, bold=True).pack(side="left")
        self.range_var = tk.StringVar(value="LAST 24 HOURS")
        range_box = ttk.Combobox(
            controls, textvariable=self.range_var, values=tuple(self._RANGES),
            state="readonly", style="Nexus.TCombobox", width=17,
        )
        range_box.pack(side="left", padx=(8, 18), ipady=5)
        range_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh())
        _label(controls, "METRIC", self.theme, muted=True, bold=True).pack(side="left")
        self.metric_var = tk.StringVar(value="cpu")
        metric_box = ttk.Combobox(
            controls, textvariable=self.metric_var,
            values=tuple(HISTORY_METRICS), state="readonly",
            style="Nexus.TCombobox", width=14,
        )
        metric_box.pack(side="left", padx=8, ipady=5)
        metric_box.bind("<<ComboboxSelected>>", lambda _event: self._metric_changed())
        self.refresh_button = NexusButton(
            controls, "LOAD HISTORY", self.refresh, self.theme, primary=True, width=132
        )
        self.refresh_button.pack(side="right")

        graph_card = RoundedCard(self.body, self.theme, height=390)
        graph_card.pack(fill="both", expand=True)
        self.graph = HistoryCanvas(graph_card.content, self.theme)
        self.graph.pack(fill="both", expand=True)

        summary_card = RoundedCard(self.body, self.theme, height=104)
        summary_card.pack(fill="x", pady=(12, 0))
        self.summary_labels: dict[str, tk.Label] = {}
        for column, key in enumerate(("samples", "average", "peak", "range")):
            cell = tk.Frame(summary_card.content, bg=self.theme.surface)
            cell.grid(row=0, column=column, sticky="nsew")
            summary_card.content.columnconfigure(column, weight=1)
            _label(cell, key.upper(), self.theme, muted=True, bold=True, size=8).pack()
            value = _label(cell, "—", self.theme, bold=True, size=13)
            value.pack(pady=(7, 0))
            self.summary_labels[key] = value
        self.samples: tuple[HistorySample, ...] = ()
        self._loading = False
        self.refresh()

    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.refresh_button.set_enabled(False)
        seconds = self._RANGES.get(self.range_var.get(), 86_400)
        since = time.time() - seconds
        self.set_status("QUERYING LOCAL HISTORY")

        def query_history() -> tuple[HistorySample, ...]:
            self.manager.history_store.flush(1.0)
            return self.manager.history_store.query(since, limit=100_000)

        self.run_background(query_history, self._complete, self._failed)

    def _complete(self, result: object) -> None:
        self._loading = False
        self.refresh_button.set_enabled(True)
        self.samples = tuple(result) if isinstance(result, (tuple, list)) else ()
        self._metric_changed()
        self.set_status(f"{len(self.samples)} LOCAL SAMPLES LOADED")

    def _failed(self, error: BaseException) -> None:
        self._loading = False
        self.refresh_button.set_enabled(True)
        self.set_status(f"HISTORY QUERY FAILED // {error}", attention=True)

    def _metric_changed(self) -> None:
        metric = self.metric_var.get()
        self.graph.set_data(self.samples, metric)
        overview = summarize(self.samples)
        unit = HISTORY_METRICS[metric][1]
        average = overview.get(f"{metric}_average")
        peak = overview.get(f"{metric}_peak")
        if metric in {"download", "upload"}:
            series = history_series(self.samples, metric)
            values = [value for _timestamp, value in series]
            average = sum(values) / len(values) if values else None
            peak = max(values) if values else None
            average_text = format_data_rate(average)
            peak_text = format_data_rate(peak)
        else:
            average_text = format_metric(average, unit, 1)
            peak_text = format_metric(peak, unit, 1)
        self.summary_labels["samples"].configure(text=str(len(self.samples)))
        self.summary_labels["average"].configure(text=average_text)
        self.summary_labels["peak"].configure(text=peak_text)
        if self.samples:
            span = max(sample.timestamp for sample in self.samples) - min(
                sample.timestamp for sample in self.samples
            )
            self.summary_labels["range"].configure(text=f"{span / 3600:.1f} h")
        else:
            self.summary_labels["range"].configure(text="0 h")


class NetworkDiagnosticsWindow(FeatureWindowController):
    def build(self) -> None:
        controls_card = RoundedCard(self.body, self.theme, height=116)
        controls_card.pack(fill="x")
        controls = controls_card.content
        _label(controls, "TARGET HOST OR IP", self.theme, muted=True, bold=True).grid(
            row=0, column=0, sticky="w"
        )
        _label(controls, "PROBES", self.theme, muted=True, bold=True).grid(
            row=0, column=1, sticky="w", padx=(12, 0)
        )
        self.target_var = tk.StringVar(value=self.manager.settings.diagnostics_host)
        self.target_entry = tk.Entry(
            controls, textvariable=self.target_var, bg=self.theme.surface_alt,
            fg=self.theme.text, insertbackground=self.theme.text,
            highlightthickness=2, highlightbackground=self.theme.border,
            highlightcolor=self.theme.accent, relief="flat", font=("Segoe UI", 10),
        )
        self.target_entry.grid(row=1, column=0, sticky="ew", pady=(5, 0), ipady=7)
        self.target_entry.bind("<Return>", lambda _event: self.run())
        self.count_var = tk.StringVar(value="5")
        ttk.Combobox(
            controls, textvariable=self.count_var, values=("3", "5", "10", "20"),
            state="readonly", style="Nexus.TCombobox", width=7,
        ).grid(row=1, column=1, padx=(12, 0), pady=(5, 0), ipady=5)
        controls.columnconfigure(0, weight=1)
        self.run_button = NexusButton(
            controls, "RUN CHECK", self.run, self.theme, primary=True, width=116
        )
        self.run_button.grid(row=1, column=2, padx=(12, 0), pady=(5, 0))
        self.cancel_button = NexusButton(
            controls, "CANCEL", self.cancel, self.theme, width=92
        )
        self.cancel_button.grid(row=1, column=3, padx=(8, 0), pady=(5, 0))
        self.cancel_button.set_enabled(False)

        summary_card = RoundedCard(self.body, self.theme, height=170)
        summary_card.pack(fill="x", pady=(12, 0))
        self.summary = tk.Text(
            summary_card.content, bg=self.theme.surface, fg=self.theme.text,
            insertbackground=self.theme.text, relief="flat", wrap="word",
            height=7, font=("Cascadia Mono", 10), state="disabled",
        )
        self.summary.pack(fill="both", expand=True)
        self._set_summary((
            "READY // Enter a hostname or IP address.",
            "The check uses ICMP where permitted and labels TCP fallback clearly.",
        ))

        sample_card = RoundedCard(self.body, self.theme, height=300)
        sample_card.pack(fill="both", expand=True, pady=(12, 0))
        self.samples, scrollbar = _tree(
            sample_card.content,
            (("sequence", "#", 55), ("method", "METHOD", 90),
             ("address", "ADDRESS", 190), ("latency", "LATENCY", 110),
             ("result", "RESULT", 330)),
            height=9,
        )
        self.samples.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._running = False
        self._cancel_event = threading.Event()
        self._last_result: DiagnosticResult | None = None

    def run(self) -> None:
        if self._running:
            return
        target = self.target_var.get().strip()
        self._cancel_event = threading.Event()
        self._running = True
        self.run_button.set_enabled(False)
        self.cancel_button.set_enabled(True)
        self.samples.delete(*self.samples.get_children())
        self.set_status("RUNNING BOUNDED NETWORK CHECK")
        count = int(self.count_var.get())
        def operation() -> DiagnosticResult:
            result = run_diagnostics(
                target, count=count, timeout=1.2, interval=0.2,
                cancel_event=self._cancel_event,
            )
            self.manager.offer_dashboard_result("diagnostics", result)
            return result

        self.run_background(
            operation,
            self._complete,
            self._failed,
        )

    def cancel(self) -> None:
        if self._running:
            self._cancel_event.set()
            self.set_status("CANCELLATION REQUESTED")

    def _complete(self, result: object) -> None:
        if isinstance(result, DiagnosticResult) and result is self._last_result:
            return
        self._running = False
        self.run_button.set_enabled(True)
        self.cancel_button.set_enabled(False)
        if not isinstance(result, DiagnosticResult):
            self._failed(TypeError("diagnostic service returned an unexpected result"))
            return
        self._last_result = result
        self.manager.latest_diagnostics = result
        if hasattr(self.manager.dashboard, "latest_diagnostics"):
            self.manager.dashboard.latest_diagnostics = result
        self._set_summary(diagnostic_summary_lines(result))
        for sample in result.samples:
            self.samples.insert(
                "", "end", iid=f"probe-{sample.sequence}",
                values=(
                    sample.sequence, sample.method.value.upper(), sample.address,
                    format_metric(sample.latency_ms, " ms", 2),
                    "OK" if sample.latency_ms is not None else (sample.error or "FAILED"),
                ),
            )
        attention = result.state not in {DiagnosticState.COMPLETE, DiagnosticState.CANCELLED}
        self.set_status(f"DIAGNOSTIC {result.state.value}", attention=attention)
        if result.state not in {DiagnosticState.INVALID_TARGET, DiagnosticState.DNS_FAILED}:
            settings = settings_with_updates(
                self.manager.settings, {"diagnostics_host": result.target}
            )
            try:
                self.manager.update_settings(settings)
            except OSError as error:
                self.set_status(f"RESULT COMPLETE // HOST SETTING NOT SAVED: {error}", attention=True)

    def _failed(self, error: BaseException) -> None:
        self._running = False
        self.run_button.set_enabled(True)
        self.cancel_button.set_enabled(False)
        self._set_summary((f"ERROR // {error}",))
        self.set_status(f"DIAGNOSTIC FAILED // {error}", attention=True)

    def _set_summary(self, lines: Iterable[str]) -> None:
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", "\n".join(lines))
        self.summary.configure(state="disabled")

    def close(self) -> None:
        self._cancel_event.set()
        super().close()


class BenchmarkWindow(FeatureWindowController):
    def build(self) -> None:
        notice = RoundedCard(self.body, self.theme, height=105)
        notice.pack(fill="x")
        _label(
            notice.content, "SHORT / SAFE / CANCELLABLE", self.theme,
            bold=True, size=11,
        ).pack(anchor="w")
        _label(
            notice.content,
            "Uses SHA-256 work, bounded memory copies, and a deleted temporary file. "
            "Scores are quick comparisons, not laboratory measurements.",
            self.theme, muted=True, anchor="w", justify="left", wraplength=850,
        ).pack(fill="x", pady=(7, 0))

        controls = tk.Frame(self.body, bg=self.theme.background)
        controls.pack(fill="x", pady=12)
        self.start_button = NexusButton(
            controls, "RUN FULL SUITE", self.start, self.theme, primary=True, width=142
        )
        self.start_button.pack(side="left")
        self.cancel_button = NexusButton(
            controls, "CANCEL", self.cancel, self.theme, width=92
        )
        self.cancel_button.pack(side="left", padx=8)
        self.cancel_button.set_enabled(False)
        self.stage_label = _label(
            controls, "READY", self.theme, muted=True, bold=True, anchor="e"
        )
        self.stage_label.pack(side="right")

        self.progress_value = tk.DoubleVar(value=0.0)
        ttk.Progressbar(
            self.body, variable=self.progress_value, maximum=100,
            style="Nexus.Horizontal.TProgressbar",
        ).pack(fill="x", pady=(0, 12), ipady=4)

        results_card = RoundedCard(self.body, self.theme, height=335)
        results_card.pack(fill="both", expand=True)
        self.results_tree, scrollbar = _tree(
            results_card.content,
            (("test", "TEST", 180), ("status", "STATUS", 110),
             ("score", "SCORE", 150), ("duration", "DURATION", 110),
             ("detail", "DETAIL", 330)),
            height=10,
        )
        self.results_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.runner = BenchmarkRunner()
        self._last_progress_at = 0.0
        self._render_results(self.manager.benchmark_results)

    def start(self) -> None:
        if self.runner.running:
            return
        self.progress_value.set(0.0)
        self.start_button.set_enabled(False)
        self.cancel_button.set_enabled(True)
        self.stage_label.configure(text="STARTING", fg=self.theme.text)
        self.set_status("BENCHMARK SUITE RUNNING")
        self._last_progress_at = 0.0

        def progress(stage: str, amount: float) -> None:
            now = time.monotonic()
            if amount < 1.0 and now - self._last_progress_at < 0.075:
                return
            self._last_progress_at = now
            self.post(self._progress, stage, amount)

        def complete(results: tuple[BenchmarkResult, ...]) -> None:
            self.manager.offer_dashboard_result("benchmarks", results)
            self.post(self._complete, results)

        started = self.runner.start(
            complete=complete,
            progress=progress,
        )
        if not started:
            self.start_button.set_enabled(True)
            self.cancel_button.set_enabled(False)
            self.set_status("A BENCHMARK IS ALREADY RUNNING", attention=True)

    def cancel(self) -> None:
        if self.runner.running:
            self.runner.cancel()
            self.stage_label.configure(text="CANCELLING", fg=self.theme.accent)
            self.set_status("CANCELLATION REQUESTED")

    def _progress(self, stage: object, amount: object) -> None:
        try:
            percent = max(0.0, min(100.0, float(amount) * 100.0))
        except (TypeError, ValueError):
            percent = 0.0
        self.stage_label.configure(text=f"{str(stage).upper()}  {percent:.0f}%")
        self.progress_value.set(percent)

    def _complete(self, results: object) -> None:
        values = tuple(results) if isinstance(results, (tuple, list)) else ()
        self.manager.benchmark_results = tuple(
            item for item in values if isinstance(item, BenchmarkResult)
        )
        if hasattr(self.manager.dashboard, "latest_benchmarks"):
            self.manager.dashboard.latest_benchmarks = self.manager.benchmark_results
        self._render_results(self.manager.benchmark_results)
        self.start_button.set_enabled(True)
        self.cancel_button.set_enabled(False)
        self.progress_value.set(100.0 if self.manager.benchmark_results else 0.0)
        cancelled = any(item.status == "cancelled" for item in self.manager.benchmark_results)
        self.stage_label.configure(
            text="CANCELLED" if cancelled else "COMPLETE",
            fg=self.theme.accent if cancelled else self.theme.text,
        )
        self.set_status(
            f"{len(self.manager.benchmark_results)} BENCHMARK RESULTS "
            + ("// CANCELLED" if cancelled else "// COMPLETE"),
            attention=cancelled,
        )
        self.manager._notify_status("benchmarks")

    def _render_results(self, results: Iterable[BenchmarkResult]) -> None:
        self.results_tree.delete(*self.results_tree.get_children())
        for index, result in enumerate(results):
            score = (
                f"{format_metric(result.score, '', 2)} {result.unit}".strip()
                if result.score is not None else "N/A"
            )
            self.results_tree.insert(
                "", "end", iid=f"benchmark-{index}",
                values=(
                    result.name, result.status.upper(), score,
                    format_metric(result.duration_seconds, " s", 3), result.detail,
                ),
            )

    def close(self) -> None:
        self.runner.cancel()
        super().close()


class ReportExportWindow(FeatureWindowController):
    def build(self) -> None:
        privacy = RoundedCard(self.body, self.theme, height=112)
        privacy.pack(fill="x")
        _label(privacy.content, "PRIVACY-FIRST EXPORT", self.theme, bold=True, size=11).pack(anchor="w")
        _label(
            privacy.content,
            "Reports are created locally. Usernames, IP addresses, serial numbers, and device "
            "paths are excluded by the report service.",
            self.theme, muted=True, anchor="w", justify="left", wraplength=850,
        ).pack(fill="x", pady=(7, 0))

        options = RoundedCard(self.body, self.theme, height=150)
        options.pack(fill="x", pady=(12, 0))
        _label(options.content, "FORMAT", self.theme, muted=True, bold=True).grid(
            row=0, column=0, sticky="w"
        )
        _label(options.content, "DESTINATION", self.theme, muted=True, bold=True).grid(
            row=0, column=1, sticky="w", padx=(18, 0)
        )
        self.format_var = tk.StringVar(value="html")
        formats = tk.Frame(options.content, bg=self.theme.surface)
        formats.grid(row=1, column=0, sticky="w", pady=(7, 0))
        for value in ("html", "json"):
            tk.Radiobutton(
                formats, text=value.upper(), variable=self.format_var, value=value,
                command=self._format_changed, bg=self.theme.surface, fg=self.theme.text,
                selectcolor=self.theme.surface_alt, activebackground=self.theme.surface,
                activeforeground=self.theme.text, font=("Segoe UI", 9, "bold"),
            ).pack(side="left", padx=(0, 8))
        destination = tk.Frame(options.content, bg=self.theme.surface)
        destination.grid(row=1, column=1, sticky="ew", padx=(18, 0), pady=(7, 0))
        options.content.columnconfigure(1, weight=1)
        self.path_var = tk.StringVar(value=str(Path.cwd() / report_default_filename("html")))
        tk.Entry(
            destination, textvariable=self.path_var, bg=self.theme.surface_alt,
            fg=self.theme.text, insertbackground=self.theme.text,
            highlightthickness=2, highlightbackground=self.theme.border,
            highlightcolor=self.theme.accent, relief="flat", font=("Segoe UI", 9),
        ).pack(side="left", fill="x", expand=True, ipady=7)
        NexusButton(destination, "BROWSE", self._browse, self.theme, width=88).pack(
            side="left", padx=(8, 0)
        )
        actions = tk.Frame(options.content, bg=self.theme.surface)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.export_button = NexusButton(
            actions, "CREATE REPORT", self._export, self.theme, primary=True, width=140
        )
        self.export_button.pack(side="left")
        NexusButton(actions, "REFRESH PREVIEW", self._preview, self.theme, width=142).pack(
            side="left", padx=8
        )

        preview_card = RoundedCard(self.body, self.theme, height=300)
        preview_card.pack(fill="both", expand=True, pady=(12, 0))
        self.preview = tk.Text(
            preview_card.content, bg=self.theme.surface_alt, fg=self.theme.text,
            insertbackground=self.theme.text, relief="flat", wrap="none",
            font=("Cascadia Mono", 9), state="disabled",
        )
        self.preview.pack(fill="both", expand=True)
        self._exporting = False
        self._preview()

    def _format_changed(self) -> None:
        current = Path(self.path_var.get().strip() or report_default_filename(self.format_var.get()))
        self.path_var.set(str(current.with_suffix(f".{self.format_var.get()}")))
        self._preview()

    def _browse(self) -> None:
        selected = self.format_var.get()
        filetypes = (
            (("HTML report", "*.html"), ("All files", "*.*"))
            if selected == "html" else (("JSON report", "*.json"), ("All files", "*.*"))
        )
        filename = filedialog.asksaveasfilename(
            parent=self.window,
            title="Save NEXUS hardware report",
            defaultextension=f".{selected}",
            initialfile=report_default_filename(selected),
            filetypes=filetypes,
        )
        if filename:
            self.path_var.set(filename)

    def _report(self) -> Mapping[str, object] | None:
        snapshot = self.manager.current_snapshot()
        if snapshot is None:
            return None
        return self.manager.build_report(snapshot)

    def _preview(self) -> None:
        report = self._report()
        if report is None:
            content = "No live snapshot is available yet. Wait for the dashboard's first reading."
            self.set_status("REPORT UNAVAILABLE // WAITING FOR SNAPSHOT", attention=True)
        else:
            content = report_json(report)
            if len(content) > 18_000:
                content = content[:18_000] + "\n… preview truncated …\n"
            self.set_status("REPORT PREVIEW READY")
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", content)
        self.preview.configure(state="disabled")

    def _export(self) -> None:
        if self._exporting:
            return
        report = self._report()
        if report is None:
            self.set_status("WAIT FOR A LIVE SNAPSHOT BEFORE EXPORTING", attention=True)
            return
        destination = self.path_var.get().strip()
        if not destination:
            self._browse()
            destination = self.path_var.get().strip()
        if not destination:
            return
        self._exporting = True
        self.export_button.set_enabled(False)
        selected_format = self.format_var.get()
        self.set_status("WRITING PRIVATE OFFLINE REPORT")
        self.run_background(
            lambda: write_report(destination, report, selected_format),
            self._export_complete,
            self._export_failed,
        )

    def _export_complete(self, result: object) -> None:
        self._exporting = False
        self.export_button.set_enabled(True)
        self.path_var.set(str(result))
        self.set_status(f"REPORT SAVED // {result}")

    def _export_failed(self, error: BaseException) -> None:
        self._exporting = False
        self.export_button.set_enabled(True)
        self.set_status(f"REPORT EXPORT FAILED // {error}", attention=True)


class CustomizationWindow(FeatureWindowController):
    def build(self) -> None:
        self._variables: dict[str, tk.Variable] = {}
        self._metric_vars: dict[str, tk.BooleanVar] = {}

        appearance = RoundedCard(self.body, self.theme, height=128)
        appearance.pack(fill="x")
        _label(appearance.content, "APPEARANCE", self.theme, bold=True, size=11).pack(anchor="w")
        row = tk.Frame(appearance.content, bg=self.theme.surface)
        row.pack(fill="x", pady=(10, 0))
        _label(row, "ACCENT", self.theme, muted=True, bold=True).pack(side="left")
        self._variables["accent"] = tk.StringVar()
        accent_box = ttk.Combobox(
            row, textvariable=self._variables["accent"],
            values=("red", "crimson", "ruby", "mono"), state="readonly",
            style="Nexus.TCombobox", width=12,
        )
        accent_box.pack(side="left", padx=(8, 22), ipady=5)
        self._variables["reduced_motion"] = tk.BooleanVar()
        tk.Checkbutton(
            row, text="REDUCED MOTION", variable=self._variables["reduced_motion"],
            bg=self.theme.surface, fg=self.theme.text, selectcolor=self.theme.surface_alt,
            activebackground=self.theme.surface, activeforeground=self.theme.text,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        _label(row, "ANIMATION SPEED", self.theme, muted=True, bold=True).pack(
            side="left", padx=(24, 8)
        )
        self._variables["animation_speed"] = tk.DoubleVar()
        tk.Scale(
            row, from_=0.25, to=3.0, resolution=0.25, orient="horizontal",
            variable=self._variables["animation_speed"], bg=self.theme.surface,
            fg=self.theme.text, troughcolor=self.theme.track,
            activebackground=self.theme.accent, highlightthickness=0,
            length=170,
        ).pack(side="left")

        dashboard = RoundedCard(self.body, self.theme, height=138)
        dashboard.pack(fill="x", pady=(12, 0))
        _label(dashboard.content, "GAMING HUD METRICS", self.theme, bold=True, size=11).pack(anchor="w")
        _label(
            dashboard.content, "Choose at least one tile. Existing order is preserved.",
            self.theme, muted=True,
        ).pack(anchor="w", pady=(2, 7))
        metric_row = tk.Frame(dashboard.content, bg=self.theme.surface)
        metric_row.pack(fill="x")
        for metric in DASHBOARD_METRICS:
            variable = tk.BooleanVar()
            self._metric_vars[metric] = variable
            tk.Checkbutton(
                metric_row, text=METRIC_LABELS.get(metric, metric.title()).upper(),
                variable=variable, bg=self.theme.surface, fg=self.theme.text,
                selectcolor=self.theme.surface_alt, activebackground=self.theme.surface,
                activeforeground=self.theme.text, font=("Segoe UI", 8, "bold"),
            ).pack(side="left", padx=(0, 12))

        behaviour = RoundedCard(self.body, self.theme, height=214)
        behaviour.pack(fill="x", pady=(12, 0))
        _label(behaviour.content, "BEHAVIOUR", self.theme, bold=True, size=11).pack(anchor="w")
        fields = tk.Frame(behaviour.content, bg=self.theme.surface)
        fields.pack(fill="x", pady=(9, 0))
        self._variables["refresh_seconds"] = tk.DoubleVar()
        self._variables["history_days"] = tk.IntVar()
        self._variables["overlay_opacity"] = tk.DoubleVar()
        for column, (field, label, low, high, resolution) in enumerate((
            ("refresh_seconds", "REFRESH SECONDS", 0.25, 5.0, 0.25),
            ("history_days", "HISTORY DAYS", 1, 365, 1),
            ("overlay_opacity", "HUD OPACITY", 0.35, 1.0, 0.05),
        )):
            cell = tk.Frame(fields, bg=self.theme.surface)
            cell.grid(row=0, column=column, sticky="ew", padx=(0, 18))
            fields.columnconfigure(column, weight=1)
            _label(cell, label, self.theme, muted=True, bold=True, size=8).pack(anchor="w")
            tk.Scale(
                cell, from_=low, to=high, resolution=resolution, orient="horizontal",
                variable=self._variables[field], bg=self.theme.surface, fg=self.theme.text,
                troughcolor=self.theme.track, activebackground=self.theme.accent,
                highlightthickness=0, length=190,
            ).pack(fill="x")
        checks = tk.Frame(fields, bg=self.theme.surface)
        checks.grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))
        for field, text in (
            ("history_enabled", "RECORD HISTORY"),
            ("alerts_enabled", "ENABLE ALERTS"),
            ("minimize_to_tray", "MINIMIZE TO TRAY"),
            ("overlay_enabled", "ENABLE OVERLAY"),
        ):
            self._variables[field] = tk.BooleanVar()
            tk.Checkbutton(
                checks, text=text, variable=self._variables[field],
                bg=self.theme.surface, fg=self.theme.text,
                selectcolor=self.theme.surface_alt, activebackground=self.theme.surface,
                activeforeground=self.theme.text, font=("Segoe UI", 8, "bold"),
            ).pack(side="left", padx=(0, 16))

        actions = tk.Frame(self.body, bg=self.theme.background)
        actions.pack(fill="x", pady=(14, 0))
        NexusButton(
            actions, "APPLY SETTINGS", self._apply, self.theme, primary=True, width=144
        ).pack(side="left")
        NexusButton(actions, "RESET FORM", self._reset, self.theme, width=112).pack(
            side="left", padx=8
        )
        _label(
            actions, "Changes are validated and atomically saved.",
            self.theme, muted=True,
        ).pack(side="right")
        self._load(self.manager.settings)

    def _load(self, settings: AppSettings) -> None:
        for name, variable in self._variables.items():
            variable.set(getattr(settings, name))
        for metric, variable in self._metric_vars.items():
            variable.set(metric in settings.dashboard_metrics)

    def _reset(self) -> None:
        self._load(AppSettings())
        self.set_status("DEFAULTS LOADED INTO FORM // APPLY TO SAVE")

    def _apply(self) -> None:
        selected_metrics = tuple(
            metric for metric in DASHBOARD_METRICS if self._metric_vars[metric].get()
        )
        if not selected_metrics:
            self.set_status("SELECT AT LEAST ONE GAMING HUD METRIC", attention=True)
            return
        updates = {
            name: variable.get() for name, variable in self._variables.items()
        }
        updates["dashboard_metrics"] = selected_metrics
        settings = settings_with_updates(self.manager.settings, updates)
        try:
            self.manager.update_settings(settings)
        except OSError as error:
            self.set_status(f"SETTINGS COULD NOT BE SAVED // {error}", attention=True)
            return
        self._load(settings)
        self.set_status("SETTINGS APPLIED AND SAVED")

    def on_settings_changed(self, settings: AppSettings) -> None:
        self._load(settings)


_CONTROLLERS: Mapping[str, type[FeatureWindowController]] = {
    "processes": ProcessExplorerWindow,
    "alerts": AlertCenterWindow,
    "sensors": SensorHealthWindow,
    "history": HistoryViewerWindow,
    "diagnostics": NetworkDiagnosticsWindow,
    "benchmarks": BenchmarkWindow,
    "reports": ReportExportWindow,
    "customization": CustomizationWindow,
}


class FeatureWindowManager:
    """Own and integrate all eight feature windows.

    ``parent`` must be a Tk widget.  ``dashboard`` is optional and is only
    inspected for the conventional cached attributes ``latest_snapshot``,
    ``hardware``, and ``latest_network``.  Explicit callbacks supplied through
    ``FeatureServices`` take precedence.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        dashboard: object | None = None,
        services: FeatureServices | None = None,
    ) -> None:
        self.parent = parent
        self.dashboard = dashboard if dashboard is not None else parent
        self.services = services or FeatureServices()
        if self.services.get_snapshot is None:
            self.services.get_snapshot = lambda: getattr(self.dashboard, "latest_snapshot", None)
        if self.services.get_hardware is None:
            self.services.get_hardware = lambda: getattr(self.dashboard, "hardware", None)
        if self.services.get_network is None:
            self.services.get_network = lambda: getattr(self.dashboard, "latest_network", None)
        if self.services.get_sensor_snapshot is None and hasattr(
            self.dashboard, "latest_sensor_snapshot"
        ):
            self.services.get_sensor_snapshot = lambda: getattr(
                self.dashboard, "latest_sensor_snapshot", None
            )
        if self.services.get_drive_health is None and hasattr(
            self.dashboard, "latest_smart_health"
        ):
            self.services.get_drive_health = lambda: getattr(
                self.dashboard, "latest_smart_health", ()
            )
        if self.services.get_benchmark_results is None and hasattr(
            self.dashboard, "latest_benchmarks"
        ):
            self.services.get_benchmark_results = lambda: getattr(
                self.dashboard, "latest_benchmarks", ()
            )

        dashboard_store = getattr(self.dashboard, "settings_store", None)
        self.settings_store = self.services.settings_store or dashboard_store or SettingsStore()
        dashboard_settings = getattr(self.dashboard, "settings", None)
        self.settings = (
            dashboard_settings if isinstance(dashboard_settings, AppSettings)
            else self.settings_store.load()
        )
        dashboard_theme = getattr(self.dashboard, "theme", None)
        self.theme = (
            dashboard_theme if isinstance(dashboard_theme, SemanticTheme)
            else resolve_theme("graphite", self.settings.accent)
        )

        dashboard_history = getattr(self.dashboard, "history_store", None)
        self._owns_history = self.services.history_store is None and not isinstance(
            dashboard_history, HistoryStore
        )
        self.history_store = self.services.history_store or dashboard_history or HistoryStore(
            retention_days=self.settings.history_days,
            autostart=self.settings.history_enabled,
        )
        dashboard_sensor_hub = getattr(self.dashboard, "sensor_hub", None)
        self._owns_sensor_hub = self.services.sensor_hub is None and not isinstance(
            dashboard_sensor_hub, SensorHub
        )
        self.sensor_hub = (
            self.services.sensor_hub or dashboard_sensor_hub
            or SensorHub(default_sensor_providers())
        )
        self.smart_runner = self.services.smart_runner or SmartctlRunner()

        dashboard_alert_engine = getattr(self.dashboard, "alert_engine", None)
        self.alert_engine = (
            dashboard_alert_engine if isinstance(dashboard_alert_engine, AlertEngine)
            else AlertEngine(alert_rules_from_settings(self.settings))
        )
        dashboard_events = getattr(self.dashboard, "alert_events", None)
        self._alert_events: deque[AlertEvent] = (
            dashboard_events if isinstance(dashboard_events, deque) else deque(maxlen=500)
        )
        dashboard_sensors = getattr(self.dashboard, "latest_sensor_snapshot", None)
        self.sensor_snapshot: SensorSnapshot | None = (
            dashboard_sensors if isinstance(dashboard_sensors, SensorSnapshot) else None
        )
        self.drive_health: tuple[SmartDeviceHealth, ...] = tuple(
            item for item in getattr(self.dashboard, "latest_smart_health", ())
            if isinstance(item, SmartDeviceHealth)
        )
        self.benchmark_results: tuple[BenchmarkResult, ...] = tuple(
            item for item in getattr(self.dashboard, "latest_benchmarks", ())
            if isinstance(item, BenchmarkResult)
        )
        self.process_snapshot: ProcessSnapshot | None = (
            getattr(self.dashboard, "latest_processes", None)
            if isinstance(getattr(self.dashboard, "latest_processes", None), ProcessSnapshot)
            else None
        )
        self.latest_diagnostics: DiagnosticResult | None = (
            getattr(self.dashboard, "latest_diagnostics", None)
            if isinstance(getattr(self.dashboard, "latest_diagnostics", None), DiagnosticResult)
            else None
        )
        self._windows: dict[str, FeatureWindowController] = {}
        self._last_history_timestamp: float | None = None
        self._latest_snapshot: object | None = None
        self._latest_network: object | None = None
        self._shutdown = False

    @property
    def alert_events(self) -> tuple[AlertEvent, ...]:
        return tuple(self._alert_events)

    @property
    def open_keys(self) -> tuple[str, ...]:
        return tuple(key for key, window in self._windows.items() if window.is_open)

    def current_snapshot(self) -> object | None:
        if self._latest_snapshot is not None:
            return self._latest_snapshot
        callback = self.services.get_snapshot
        return callback() if callback is not None else None

    def current_hardware(self) -> object | None:
        callback = self.services.get_hardware
        return callback() if callback is not None else None

    def current_network(self) -> object | None:
        if self._latest_network is not None:
            return self._latest_network
        callback = self.services.get_network
        return callback() if callback is not None else None

    def current_temperature(self) -> float | None:
        dashboard_value = getattr(self.dashboard, "latest_temperature_c", None)
        if dashboard_value is not None:
            try:
                value = float(dashboard_value)
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if math.isfinite(value):
                    return value
        snapshot = self.sensor_snapshot
        if snapshot is None:
            callback = self.services.get_sensor_snapshot
            snapshot = callback() if callback is not None else None
        if snapshot is None:
            return None
        values = [
            reading.value for reading in snapshot.readings
            if reading.kind.value == "temperature" and math.isfinite(reading.value)
        ]
        return max(values) if values else None

    def open(self, feature: object) -> FeatureWindowController:
        """Open or focus one feature and return its controller."""

        if self._shutdown:
            raise RuntimeError("feature window manager has been shut down")
        spec = feature_window_spec(feature)
        existing = self._windows.get(spec.key)
        if existing is not None and existing.is_open:
            existing.window.deiconify()
            existing.window.lift()
            try:
                existing.window.focus_force()
            except tk.TclError:
                pass
            return existing
        controller = _CONTROLLERS[spec.key](self, spec)
        self._windows[spec.key] = controller
        self._notify_status(spec.key)
        return controller

    # Alias reads naturally when handed directly to a launcher.
    open_feature = open

    def callbacks(self) -> dict[str, Callable[[], None]]:
        """Return callbacks keyed exactly like ``v4_control_center.FEATURES``."""

        return {
            spec.key: (lambda key=spec.key: self.open(key))
            for spec in FEATURE_WINDOW_CATALOG
        }

    def attach_to_hub(self, hub: object) -> None:
        """Register every callback on a ``v4_control_center.NexusLabHub``."""

        setter = getattr(hub, "set_callback", None)
        if not callable(setter):
            raise TypeError("hub must provide set_callback(feature, callback)")
        for key, callback in self.callbacks().items():
            setter(key, callback)

    def refresh(self, feature: object, payload: object = None) -> bool:
        """Accept a dashboard worker result and refresh an open window.

        The method is intentionally cheap and must be called on the Tk thread;
        ``HardwareDashboard._poll_results`` is the expected integration point.
        It still caches useful payloads when the corresponding window is shut.
        """

        raw_key = str(feature).strip().casefold().replace("-", "_").replace(" ", "_")
        key = normalize_window_key(raw_key)
        if raw_key in {"smart_health", "smart_scan", "drive_health"}:
            key = "sensors"
        if key not in _CONTROLLERS:
            return False

        if key == "processes" and isinstance(payload, ProcessSnapshot):
            self.process_snapshot = payload
            if hasattr(self.dashboard, "latest_processes"):
                self.dashboard.latest_processes = payload
        elif key == "alerts" and isinstance(payload, Iterable) and not isinstance(
            payload, (str, bytes, Mapping)
        ):
            dashboard_engine = getattr(self.dashboard, "alert_engine", None)
            if isinstance(dashboard_engine, AlertEngine):
                self.alert_engine = dashboard_engine
            incoming = tuple(item for item in payload if isinstance(item, AlertEvent))
            if self._alert_events is not getattr(self.dashboard, "alert_events", None):
                self._alert_events.clear()
                self._alert_events.extend(incoming)
        elif key == "sensors":
            if isinstance(payload, SensorSnapshot):
                self.sensor_snapshot = payload
                if hasattr(self.dashboard, "latest_sensor_snapshot"):
                    self.dashboard.latest_sensor_snapshot = payload
            elif isinstance(payload, SmartScanResult):
                pass
            elif isinstance(payload, (tuple, list)):
                health = tuple(item for item in payload if isinstance(item, SmartDeviceHealth))
                if health or not payload:
                    self.drive_health = health
                    if hasattr(self.dashboard, "latest_smart_health"):
                        self.dashboard.latest_smart_health = health
        elif key == "diagnostics" and isinstance(payload, DiagnosticResult):
            self.latest_diagnostics = payload
            if hasattr(self.dashboard, "latest_diagnostics"):
                self.dashboard.latest_diagnostics = payload
        elif key == "benchmarks" and isinstance(payload, (tuple, list)):
            self.benchmark_results = tuple(
                item for item in payload if isinstance(item, BenchmarkResult)
            )
            if hasattr(self.dashboard, "latest_benchmarks"):
                self.dashboard.latest_benchmarks = self.benchmark_results
        elif key == "customization" and isinstance(payload, AppSettings):
            self.settings = payload
            dashboard_history = getattr(self.dashboard, "history_store", None)
            if isinstance(dashboard_history, HistoryStore):
                self.history_store = dashboard_history
            dashboard_engine = getattr(self.dashboard, "alert_engine", None)
            if isinstance(dashboard_engine, AlertEngine):
                self.alert_engine = dashboard_engine
            dashboard_theme = getattr(self.dashboard, "theme", None)
            if isinstance(dashboard_theme, SemanticTheme):
                self.theme = dashboard_theme

        controller = self._windows.get(key)
        if controller is None or not controller.is_open:
            self._notify_status(key)
            return False
        if isinstance(controller, ProcessExplorerWindow) and isinstance(payload, ProcessSnapshot):
            controller.snapshot = payload
            controller._render()
        elif isinstance(controller, AlertCenterWindow):
            controller._render_events()
        elif isinstance(controller, SensorHealthWindow):
            if isinstance(payload, SensorSnapshot):
                controller._render_sensors(payload)
            elif isinstance(payload, (tuple, list)):
                scan = SmartScanResult(
                    SmartCapability.AVAILABLE,
                    detail="Drive health supplied by the dashboard.",
                )
                controller._render_smart(scan, self.drive_health)
        elif isinstance(controller, NetworkDiagnosticsWindow) and isinstance(
            payload, DiagnosticResult
        ):
            controller._complete(payload)
        elif isinstance(controller, BenchmarkWindow) and isinstance(payload, (tuple, list)):
            controller._render_results(self.benchmark_results)
        elif isinstance(controller, ReportExportWindow):
            controller._preview()
        elif isinstance(controller, CustomizationWindow) and isinstance(payload, AppSettings):
            controller.on_settings_changed(payload)
        self._notify_status(key)
        return True

    def offer_dashboard_result(self, kind: str, result: object) -> bool:
        """Put a worker result on the dashboard queue when that bridge exists."""

        offer = getattr(self.dashboard, "_offer_result", None)
        if not callable(offer):
            return False
        try:
            offer(kind, result)
        except Exception:
            return False
        return True

    def status_catalog(self) -> dict[str, dict[str, object]]:
        """Return hub-compatible plain status mappings."""

        active = len(self.alert_engine.active_keys)
        alerts_paused = bool(getattr(self.dashboard, "alerts_paused", False))
        snapshot = self.current_snapshot()
        statuses: dict[str, dict[str, object]] = {
            spec.key: {"state": "ready", "detail": "Ready"}
            for spec in FEATURE_WINDOW_CATALOG
        }
        statuses["alerts"] = {
            "state": "attention" if active or alerts_paused else "ready",
            "detail": (
                "Paused"
                if alerts_paused
                else f"{active} active warning(s)" if active else "Thresholds ready"
            ),
            "count": active or None,
        }
        statuses["processes"] = {
            "state": "ready",
            "detail": (
                f"{len(self.process_snapshot.processes)} process(es) visible"
                if self.process_snapshot is not None else "Waiting for first process sample"
            ),
        }
        statuses["sensors"] = {
            "state": "ready",
            "detail": (
                f"{len(self.sensor_snapshot.readings)} sensor value(s)"
                if self.sensor_snapshot is not None else "Optional providers checked on demand"
            ),
        }
        statuses["benchmarks"] = {
            "state": "ready",
            "detail": f"{len(self.benchmark_results)} result(s)" if self.benchmark_results else "Ready",
        }
        statuses["reports"] = {
            "state": "ready" if snapshot is not None else "unavailable",
            "detail": "Snapshot ready" if snapshot is not None else "Waiting for first snapshot",
        }
        if self.latest_diagnostics is not None:
            statuses["diagnostics"] = {
                "state": (
                    "ready" if self.latest_diagnostics.state is DiagnosticState.COMPLETE
                    else "attention"
                ),
                "detail": f"Last result: {self.latest_diagnostics.state.value}",
            }
        if not self.settings.history_enabled:
            statuses["history"] = {
                "state": "unavailable", "detail": "Local history is disabled"
            }
        if self.history_store.error is not None:
            statuses["history"] = {"state": "error", "detail": str(self.history_store.error)}
        for key in self.open_keys:
            if statuses[key]["state"] == "ready":
                statuses[key] = {**statuses[key], "detail": "Window open"}
        return statuses

    def ingest_snapshot(
        self,
        snapshot: object,
        *,
        temperature_c: float | None = None,
        sensor_snapshot: SensorSnapshot | None = None,
        network: object | None = None,
    ) -> tuple[AlertEvent, ...]:
        """Feed one dashboard sample into history and alert state.

        A dashboard normally calls this once after rendering each fresh
        snapshot.  Duplicate history timestamps are suppressed.
        """

        self._latest_snapshot = snapshot
        if sensor_snapshot is not None:
            self.sensor_snapshot = sensor_snapshot
        if network is not None:
            self._latest_network = network
        temperature = temperature_c if temperature_c is not None else self.current_temperature()
        events = self.evaluate_alerts(snapshot, temperature)
        try:
            captured_at = float(getattr(snapshot, "captured_at"))
        except (AttributeError, TypeError, ValueError, OverflowError):
            captured_at = time.time()
        if self.settings.history_enabled and captured_at != self._last_history_timestamp:
            rates = network if network is not None else self.current_network()
            self.history_store.add_snapshot(
                snapshot,
                temperature_c=temperature,
                network_down_bps=getattr(rates, "download_bps", None),
                network_up_bps=getattr(rates, "upload_bps", None),
            )
            self._last_history_timestamp = captured_at
        return events

    def evaluate_alerts(
        self, snapshot: object, temperature_c: float | None = None
    ) -> tuple[AlertEvent, ...]:
        if not self.settings.alerts_enabled or bool(
            getattr(self.dashboard, "alerts_paused", False)
        ):
            return ()
        events = self.alert_engine.evaluate(alert_metrics_from_snapshot(snapshot, temperature_c))
        for event in reversed(events):
            self._alert_events.appendleft(event)
        if events:
            self._notify_status("alerts")
        return events

    def resolve_alerts(self) -> tuple[AlertEvent, ...]:
        events = self.alert_engine.resolve_all()
        for event in reversed(events):
            self._alert_events.appendleft(event)
        self._notify_status("alerts")
        return events

    def update_settings(self, settings: AppSettings) -> AppSettings:
        """Validate, persist, and broadcast new settings."""

        validated = AppSettings.from_mapping(settings.as_dict())
        old_engine = self.alert_engine
        dashboard_apply = getattr(self.dashboard, "apply_settings", None)
        if callable(dashboard_apply):
            applied = dashboard_apply(validated)
            if isinstance(applied, AppSettings):
                validated = applied
            self.settings_store = getattr(self.dashboard, "settings_store", self.settings_store)
            self.history_store = getattr(self.dashboard, "history_store", self.history_store)
            self.alert_engine = getattr(
                self.dashboard, "alert_engine", AlertEngine(alert_rules_from_settings(validated))
            )
            dashboard_theme = getattr(self.dashboard, "theme", None)
            self.theme = (
                dashboard_theme if isinstance(dashboard_theme, SemanticTheme)
                else resolve_theme("graphite", validated.accent)
            )
        else:
            self.settings_store.save(validated)
            self.theme = resolve_theme("graphite", validated.accent)
            self.alert_engine = AlertEngine(alert_rules_from_settings(validated))
            self.history_store.retention_days = validated.history_days
            if validated.history_enabled:
                self.history_store.start()
        old_events = old_engine.resolve_all()
        for event in reversed(old_events):
            self._alert_events.appendleft(event)
        self.settings = validated
        for controller in tuple(self._windows.values()):
            if controller.is_open:
                controller.on_settings_changed(validated)
        callback = self.services.on_settings_changed
        if callback is not None and callback != dashboard_apply:
            callback(validated)
        self._notify_status("customization")
        self._notify_status("alerts")
        return validated

    def collect_health(
        self,
    ) -> tuple[SensorSnapshot, SmartScanResult, tuple[SmartDeviceHealth, ...]]:
        sensor_callback = self.services.get_sensor_snapshot
        sensors = sensor_callback() if sensor_callback is not None else None
        if sensors is None:
            sensors = self.sensor_hub.sample()
        health_callback = self.services.get_drive_health
        cached_health = tuple(health_callback()) if health_callback is not None else ()
        if cached_health:
            health = cached_health
            scan = SmartScanResult(
                SmartCapability.AVAILABLE,
                detail="Drive health supplied by the dashboard callback.",
            )
        else:
            scan = self.smart_runner.scan()
            health = self.smart_runner.poll_all(scan.devices) if scan.devices else ()
        self.sensor_snapshot = sensors
        self.drive_health = health
        self.offer_dashboard_result("smart_health", health)
        return sensors, scan, health

    def build_report(self, snapshot: object | None = None) -> dict[str, Any]:
        source = snapshot if snapshot is not None else self.current_snapshot()
        if source is None:
            raise ValueError("a live snapshot is required to build a report")
        sensor_snapshot = self.sensor_snapshot
        if sensor_snapshot is None and self.services.get_sensor_snapshot is not None:
            sensor_snapshot = self.services.get_sensor_snapshot()
        health = self.drive_health
        if not health and self.services.get_drive_health is not None:
            health = tuple(self.services.get_drive_health())
        benchmark_results = self.benchmark_results
        if not benchmark_results and self.services.get_benchmark_results is not None:
            benchmark_results = tuple(self.services.get_benchmark_results())
        sensor_rows = (
            ({
                "label": reading.label,
                "kind": reading.kind.value,
                "value": reading.value,
                "unit": reading.unit,
            } for reading in sensor_snapshot.readings)
            if sensor_snapshot is not None else ()
        )
        health_rows = (
            {
                "model": item.model or item.device.info_name or "Drive",
                "status": item.health.value,
                "temperature_c": item.temperature_c,
                "power_on_hours": item.power_on_hours,
                "percentage_used": item.percentage_used,
            }
            for item in health
        )
        return build_report(
            source,
            hardware=self.current_hardware(),
            sensors=sensor_rows,
            drive_health=health_rows,
            benchmarks=benchmark_results,
        )

    def _notify_status(self, key: str) -> None:
        callback = self.services.on_status_changed
        if callback is None:
            hub = getattr(self.dashboard, "lab_hub", None)
            setter = getattr(hub, "set_status", None)
            if callable(setter):
                callback = setter
        if callback is None:
            return
        status = self.status_catalog().get(key, {"state": "ready", "detail": "Ready"})
        try:
            callback(key, status)
        except Exception:
            # A status bridge is advisory and must never take down monitoring.
            return

    def _window_closed(self, key: str, controller: FeatureWindowController) -> None:
        if self._windows.get(key) is controller:
            self._windows.pop(key, None)
        self._notify_status(key)

    def close_all(self) -> None:
        for controller in tuple(self._windows.values()):
            controller.close()
        self._windows.clear()

    def shutdown(self) -> None:
        """Close windows and only the service objects created by this manager."""

        if self._shutdown:
            return
        self._shutdown = True
        self.close_all()
        if self._owns_sensor_hub:
            self.sensor_hub.close()
        if self._owns_history:
            self.history_store.close(timeout=2.0)

    # Dashboard shutdown convention.
    close = shutdown


__all__ = [
    "AlertCenterWindow",
    "BenchmarkWindow",
    "CustomizationWindow",
    "FEATURE_WINDOW_CATALOG",
    "FeatureServices",
    "FeatureWindowController",
    "FeatureWindowManager",
    "FeatureWindowSpec",
    "HISTORY_METRICS",
    "HistoryViewerWindow",
    "NetworkDiagnosticsWindow",
    "ProcessExplorerWindow",
    "ReportExportWindow",
    "SensorHealthWindow",
    "alert_metrics_from_snapshot",
    "alert_rules_from_settings",
    "diagnostic_summary_lines",
    "feature_window_spec",
    "format_data_rate",
    "format_metric",
    "history_series",
    "normalize_window_key",
    "process_rows",
    "report_default_filename",
    "scale_history_points",
    "settings_with_updates",
]
