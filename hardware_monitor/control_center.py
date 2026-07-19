"""NEXUS LAB feature registry and reusable Tk control-center hub.

The pure catalog/navigation functions make feature availability testable on
Windows and Linux without opening Tk.  ``NexusLabHub`` is intentionally only a
launcher: process collection, alerts, sensors, history, diagnostics,
benchmarks, and report generation remain in their own service modules.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping, Sequence

try:
    from .theme import SemanticTheme, resolve_theme
except ImportError:
    from v4_theme import SemanticTheme, resolve_theme


LAB_GROUPS = ("all", "monitor", "insights", "tools", "customize")
LAB_GROUP_LABELS: Mapping[str, str] = {
    "all": "ALL FEATURES",
    "monitor": "MONITOR",
    "insights": "INSIGHTS",
    "tools": "TOOLS",
    "customize": "CUSTOMIZE",
}
STATUS_STATES = ("ready", "running", "attention", "unavailable", "error", "beta")
_FEATURE_ALIASES = {
    "process": "processes",
    "process_explorer": "processes",
    "alert": "alerts",
    "smart": "sensors",
    "drive_health": "sensors",
    "network_diagnostics": "diagnostics",
    "benchmark": "benchmarks",
    "report": "reports",
    "settings": "customization",
    "appearance": "customization",
}


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    key: str
    title: str
    description: str
    group: str
    action: str
    keywords: tuple[str, ...] = ()
    number: str = "00"

    def __post_init__(self) -> None:
        if not self.key or any(character.isspace() for character in self.key):
            raise ValueError("feature keys must be non-empty and contain no spaces")
        if self.group not in LAB_GROUPS[1:]:
            raise ValueError(f"unknown feature group: {self.group!r}")

    @property
    def search_text(self) -> str:
        return " ".join((self.key, self.title, self.description, *self.keywords)).casefold()


FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        "processes", "PROCESS EXPLORER",
        "Find the applications currently using the most processor time and memory.",
        "monitor", "OPEN PROCESSES", ("apps", "programs", "cpu", "memory", "ram"), "01",
    ),
    FeatureSpec(
        "alerts", "ALERT CENTER",
        "Configure thresholds, acknowledge active warnings, and review recent events.",
        "insights", "OPEN ALERTS", ("threshold", "warning", "notification", "event"), "02",
    ),
    FeatureSpec(
        "sensors", "SENSORS & DRIVE HEALTH",
        "Inspect available temperatures, fans, power readings, and SMART-reported drive health.",
        "monitor", "OPEN HEALTH", ("temperature", "fan", "smart", "ssd", "hdd", "wear"), "03",
    ),
    FeatureSpec(
        "history", "HISTORY VAULT",
        "Explore locally stored performance history across previous sessions and time ranges.",
        "insights", "OPEN HISTORY", ("sqlite", "timeline", "session", "logging", "chart"), "04",
    ),
    FeatureSpec(
        "diagnostics", "NETWORK DIAGNOSTICS",
        "Run explicit DNS, reachability, latency, jitter, and connection checks.",
        "tools", "RUN DIAGNOSTICS", ("ping", "dns", "latency", "jitter", "loss", "tcp"), "05",
    ),
    FeatureSpec(
        "benchmarks", "BENCHMARK CENTER",
        "Run cancellable processor, memory, and temporary-file checks with saved results.",
        "tools", "OPEN BENCHMARKS", ("test", "score", "cpu", "memory", "disk"), "06",
    ),
    FeatureSpec(
        "reports", "HARDWARE REPORTS",
        "Export a private offline HTML or JSON report from the latest trusted readings.",
        "tools", "CREATE REPORT", ("export", "html", "json", "system", "inventory"), "07",
    ),
    FeatureSpec(
        "customization", "CUSTOMIZATION STUDIO",
        "Choose themes, dashboard metrics, reduced motion, tray behavior, and overlay layout.",
        "customize", "CUSTOMIZE NEXUS", ("theme", "layout", "dashboard", "tray", "overlay", "colour"), "08",
    ),
)


def normalize_feature_key(value: object) -> str:
    key = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    return _FEATURE_ALIASES.get(key, key)


def feature_by_key(key: object, features: Sequence[FeatureSpec] = FEATURES) -> FeatureSpec:
    normalized = normalize_feature_key(key)
    for feature in features:
        if feature.key == normalized:
            return feature
    raise KeyError(f"unknown NEXUS LAB feature: {key!r}")


@dataclass(frozen=True, slots=True)
class FeatureStatus:
    state: str = "ready"
    detail: str = "Ready"
    count: int | None = None

    def __post_init__(self) -> None:
        if self.state not in STATUS_STATES:
            raise ValueError(f"unknown feature status: {self.state!r}")
        if self.count is not None and self.count < 0:
            raise ValueError("status count cannot be negative")


def normalize_status(value: object) -> FeatureStatus:
    if isinstance(value, FeatureStatus):
        return value
    if isinstance(value, str):
        state = value.strip().casefold()
        return FeatureStatus(state=state, detail=state.replace("_", " ").title())
    if isinstance(value, Mapping):
        return FeatureStatus(
            state=str(value.get("state", "ready")).strip().casefold(),
            detail=str(value.get("detail", "Ready")).strip() or "Ready",
            count=(None if value.get("count") is None else int(value["count"])),
        )
    if value is None:
        return FeatureStatus()
    raise TypeError(f"unsupported feature status: {type(value).__name__}")


def status_badge(value: object) -> tuple[str, str]:
    """Return compact badge text and a semantic colour role."""
    status = normalize_status(value)
    role_by_state = {
        "ready": "text",
        "running": "accent",
        "attention": "danger",
        "unavailable": "muted",
        "error": "danger",
        "beta": "warning",
    }
    if status.count:
        label = f"{status.count} {status.state.upper()}"
    else:
        label = status.state.upper()
    return label, role_by_state[status.state]


def normalize_statuses(
    statuses: Mapping[object, object] | None,
    features: Sequence[FeatureSpec] = FEATURES,
) -> dict[str, FeatureStatus]:
    result = {feature.key: FeatureStatus() for feature in features}
    for raw_key, raw_status in (statuses or {}).items():
        key = normalize_feature_key(raw_key)
        if key in result:
            result[key] = normalize_status(raw_status)
    return result


def filter_features(
    features: Sequence[FeatureSpec] = FEATURES,
    *,
    query: object = "",
    group: object = "all",
    statuses: Mapping[object, object] | None = None,
    include_unavailable: bool = True,
) -> tuple[FeatureSpec, ...]:
    group_key = str(group).strip().casefold()
    if group_key not in LAB_GROUPS:
        group_key = "all"
    tokens = tuple(token for token in str(query).casefold().split() if token)
    normalized_statuses = normalize_statuses(statuses, features)
    result = []
    for feature in features:
        if group_key != "all" and feature.group != group_key:
            continue
        if tokens and not all(token in feature.search_text for token in tokens):
            continue
        if not include_unavailable and normalized_statuses[feature.key].state == "unavailable":
            continue
        result.append(feature)
    return tuple(result)


def group_counts(
    features: Sequence[FeatureSpec] = FEATURES,
    *,
    query: object = "",
) -> dict[str, int]:
    matching = filter_features(features, query=query)
    counts = {group: 0 for group in LAB_GROUPS}
    counts["all"] = len(matching)
    for feature in matching:
        counts[feature.group] += 1
    return counts


def responsive_columns(width: object) -> int:
    try:
        available = int(width)
    except (TypeError, ValueError):
        available = 0
    return 1 if available < 720 else 2 if available < 1260 else 3


def feature_rows(
    features: Iterable[FeatureSpec], columns: int
) -> tuple[tuple[FeatureSpec, ...], ...]:
    columns = max(1, int(columns))
    values = tuple(features)
    return tuple(values[index:index + columns] for index in range(0, len(values), columns))


@dataclass(frozen=True, slots=True)
class LabNavigationState:
    query: str = ""
    group: str = "all"
    selected: str | None = None

    def with_query(self, query: object) -> "LabNavigationState":
        return replace(self, query=str(query).strip())

    def with_group(self, group: object) -> "LabNavigationState":
        key = str(group).strip().casefold()
        return replace(self, group=key if key in LAB_GROUPS else "all")

    def with_selection(
        self, key: object | None, features: Sequence[FeatureSpec] = FEATURES
    ) -> "LabNavigationState":
        if key is None:
            return replace(self, selected=None)
        feature = feature_by_key(key, features)
        return replace(self, selected=feature.key)

    def visible(
        self,
        features: Sequence[FeatureSpec] = FEATURES,
        statuses: Mapping[object, object] | None = None,
    ) -> tuple[FeatureSpec, ...]:
        return filter_features(
            features, query=self.query, group=self.group, statuses=statuses
        )


def _rounded(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    **options,
) -> int:
    radius = max(2.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = (
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    )
    return canvas.create_polygon(points, smooth=True, splinesteps=28, **options)


class _FeatureCard(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        feature: FeatureSpec,
        status: FeatureStatus,
        command: Callable[[], None],
        theme: SemanticTheme,
    ) -> None:
        super().__init__(
            parent, height=166, bg=theme.background, bd=0,
            highlightthickness=0, takefocus=1, cursor="hand2",
        )
        self.feature = feature
        self.status = status
        self.command = command
        self.theme = theme
        self.hovered = False
        self.bind("<Configure>", lambda _event: self.draw())
        self.bind("<Enter>", lambda _event: self._hover(True))
        self.bind("<Leave>", lambda _event: self._hover(False))
        self.bind("<Button-1>", lambda _event: self.activate())
        self.bind("<Return>", lambda _event: self.activate())
        self.bind("<space>", lambda _event: self.activate())

    def _hover(self, value: bool) -> None:
        self.hovered = value
        self.draw()

    def activate(self) -> str:
        if self.status.state != "unavailable":
            self.command()
        return "break"

    def draw(self) -> None:
        width, height = max(260, self.winfo_width()), max(150, self.winfo_height())
        self.delete("all")
        theme = self.theme
        badge, role = status_badge(self.status)
        badge_colour = getattr(theme, role)
        outline = theme.accent if self.hovered else theme.border
        if self.status.state == "unavailable":
            outline = theme.border
        _rounded(
            self, 2, 2, width - 2, height - 2, 20,
            fill=theme.surface, outline=outline, width=3 if self.hovered else 2,
        )
        self.create_text(
            18, 18, anchor="nw", text=self.feature.number,
            fill=theme.accent, font=("Segoe UI", 9, "bold"),
        )
        self.create_text(
            18, 44, anchor="nw", text=self.feature.title,
            fill=theme.text, font=("Segoe UI", 13, "bold"),
        )
        self.create_text(
            18, 72, anchor="nw", width=max(180, width - 36),
            text=self.feature.description, fill=theme.muted,
            font=("Segoe UI", 9),
        )
        badge_width = max(65, len(badge) * 7 + 20)
        _rounded(
            self, 18, height - 39, 18 + badge_width, height - 15, 10,
            fill=theme.surface_alt, outline=badge_colour, width=2,
        )
        self.create_text(
            18 + badge_width / 2, height - 27, text=badge,
            fill=badge_colour, font=("Segoe UI", 7, "bold"),
        )
        action_colour = theme.muted if self.status.state == "unavailable" else theme.accent
        self.create_text(
            width - 19, height - 27, anchor="e",
            text=self.feature.action + "  →", fill=action_colour,
            font=("Segoe UI", 8, "bold"),
        )


class NexusLabHub(tk.Frame):
    """Responsive feature launcher suitable for a hidden Notebook page."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        callbacks: Mapping[str, Callable[[], None]] | None = None,
        statuses: Mapping[object, object] | None = None,
        features: Sequence[FeatureSpec] = FEATURES,
        theme: SemanticTheme | None = None,
        on_select: Callable[[str], None] | None = None,
    ) -> None:
        self.theme = theme or resolve_theme()
        super().__init__(parent, bg=self.theme.background, bd=0, highlightthickness=0)
        self.features = tuple(features)
        self.callbacks = {
            normalize_feature_key(key): callback for key, callback in (callbacks or {}).items()
        }
        self.statuses = normalize_statuses(statuses, self.features)
        self.on_select = on_select
        self.state_model = LabNavigationState()
        self._columns = 0
        self._cards: list[_FeatureCard] = []

        self._build_header()
        self._build_filters()
        self._build_viewport()
        self.refresh()

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=self.theme.background)
        header.pack(fill="x", padx=4, pady=(2, 12))
        title = tk.Frame(header, bg=self.theme.background)
        title.pack(side="left", fill="x", expand=True)
        tk.Label(
            title, text="NEXUS LAB", bg=self.theme.background, fg=self.theme.text,
            font=("Segoe UI", 21, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title, text="PROCESS INSIGHT // HEALTH // HISTORY // TOOLS // CUSTOMIZATION",
            bg=self.theme.background, fg=self.theme.accent,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(2, 0))
        search_shell = tk.Frame(
            header, bg=self.theme.surface, highlightbackground=self.theme.border,
            highlightcolor=self.theme.accent, highlightthickness=2,
        )
        search_shell.pack(side="right", padx=(15, 0), pady=6)
        self.query_var = tk.StringVar()
        self.query_var.trace_add("write", lambda *_args: self.set_query(self.query_var.get(), sync=False))
        self.search_entry = tk.Entry(
            search_shell, textvariable=self.query_var, width=27,
            bg=self.theme.surface, fg=self.theme.text, insertbackground=self.theme.accent,
            relief="flat", bd=0, font=("Segoe UI", 9),
        )
        self.search_entry.pack(padx=11, pady=8)
        self.search_entry.insert(0, "")

    def _build_filters(self) -> None:
        self.filter_bar = tk.Frame(self, bg=self.theme.background)
        self.filter_bar.pack(fill="x", pady=(0, 10))
        self.filter_buttons: dict[str, tk.Button] = {}
        for group in LAB_GROUPS:
            button = tk.Button(
                self.filter_bar, text=LAB_GROUP_LABELS[group],
                command=lambda key=group: self.set_group(key),
                bg=self.theme.surface, fg=self.theme.muted,
                activebackground=self.theme.surface_alt, activeforeground=self.theme.text,
                relief="flat", bd=0, padx=13, pady=7,
                font=("Segoe UI", 8, "bold"), cursor="hand2",
            )
            button.pack(side="left", padx=(0, 6))
            self.filter_buttons[group] = button
        self._update_filter_buttons()

    def _build_viewport(self) -> None:
        shell = tk.Frame(self, bg=self.theme.background)
        shell.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(shell, bg=self.theme.background, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(shell, orient="vertical", command=self.canvas.yview)
        self.grid_frame = tk.Frame(self.canvas, bg=self.theme.background)
        self._window = self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind("<Configure>", self._resize)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self._bind_wheel_widget(self.canvas)
        self._bind_wheel_widget(self.grid_frame)

    def _bind_wheel_widget(self, widget: tk.Misc) -> None:
        # Bind only this hub's widgets.  bind_all/unbind_all would trample
        # unrelated mouse-wheel handlers in the host dashboard.
        widget.bind("<MouseWheel>", self._wheel, add="+")
        widget.bind("<Button-4>", self._wheel, add="+")
        widget.bind("<Button-5>", self._wheel, add="+")

    def _wheel(self, event: tk.Event) -> None:
        if getattr(event, "num", None) == 4:
            units = -3
        elif getattr(event, "num", None) == 5:
            units = 3
        else:
            units = -int(event.delta / 120) * 3 if event.delta else 0
        if units:
            self.canvas.yview_scroll(units, "units")

    def _resize(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        columns = responsive_columns(event.width)
        if columns != self._columns:
            self._columns = columns
            self._layout_cards()

    def _update_filter_buttons(self) -> None:
        for group, button in self.filter_buttons.items():
            selected = group == self.state_model.group
            button.configure(
                bg=self.theme.accent_dim if selected else self.theme.surface,
                fg=self.theme.text if selected else self.theme.muted,
                highlightthickness=2 if selected else 0,
                highlightbackground=self.theme.accent,
            )

    def _layout_cards(self) -> None:
        columns = self._columns or responsive_columns(self.canvas.winfo_width())
        for card in self._cards:
            card.grid_forget()
        for column in range(3):
            self.grid_frame.columnconfigure(
                column, weight=1 if column < columns else 0,
                uniform="nexus-lab" if column < columns else "",
            )
        for index, card in enumerate(self._cards):
            row, column = divmod(index, columns)
            card.grid(row=row, column=column, sticky="nsew", padx=5, pady=5)

    def refresh(self) -> tuple[FeatureSpec, ...]:
        visible = self.state_model.visible(self.features, self.statuses)
        for card in self._cards:
            card.destroy()
        self._cards.clear()
        for feature in visible:
            card = _FeatureCard(
                self.grid_frame, feature, self.statuses[feature.key],
                lambda key=feature.key: self.open_feature(key), self.theme,
            )
            self._bind_wheel_widget(card)
            self._cards.append(card)
        if not self._cards:
            empty = FeatureSpec(
                "no_results", "NO MATCHING FEATURES",
                "Try a shorter search or choose ALL FEATURES.",
                "tools", "CLEAR SEARCH", (), "--",
            )
            self._cards.append(_FeatureCard(
                self.grid_frame, empty, FeatureStatus("unavailable", "No results"),
                lambda: None, self.theme,
            ))
        self._layout_cards()
        self._update_filter_buttons()
        return visible

    def set_query(self, query: object, *, sync: bool = True) -> tuple[FeatureSpec, ...]:
        self.state_model = self.state_model.with_query(query)
        if sync and self.query_var.get() != self.state_model.query:
            self.query_var.set(self.state_model.query)
            # The StringVar trace performed the refresh synchronously.
            return self.state_model.visible(self.features, self.statuses)
        return self.refresh()

    def set_group(self, group: object) -> tuple[FeatureSpec, ...]:
        self.state_model = self.state_model.with_group(group)
        return self.refresh()

    def set_status(self, feature: object, status: object) -> FeatureStatus:
        key = feature_by_key(feature, self.features).key
        normalized = normalize_status(status)
        self.statuses[key] = normalized
        self.refresh()
        return normalized

    def set_statuses(self, statuses: Mapping[object, object]) -> None:
        # This is a patch operation: callers can update one provider without
        # resetting every other feature to READY.
        for raw_key, raw_status in statuses.items():
            try:
                key = feature_by_key(raw_key, self.features).key
            except KeyError:
                continue
            self.statuses[key] = normalize_status(raw_status)
        self.refresh()

    def set_callback(self, feature: object, callback: Callable[[], None]) -> None:
        key = feature_by_key(feature, self.features).key
        self.callbacks[key] = callback

    def open_feature(self, feature: object) -> bool:
        spec = feature_by_key(feature, self.features)
        if self.statuses[spec.key].state == "unavailable":
            return False
        self.state_model = self.state_model.with_selection(spec.key, self.features)
        callback = self.callbacks.get(spec.key)
        if callback is not None:
            callback()
        if self.on_select is not None:
            self.on_select(spec.key)
        return callback is not None or self.on_select is not None

    def set_theme(self, theme: SemanticTheme) -> None:
        self.theme = theme
        self.configure(bg=theme.background)
        self.destroy_children_for_theme()

    def destroy_children_for_theme(self) -> None:
        """Rebuild the lightweight hub after a semantic theme change."""
        for child in self.winfo_children():
            child.destroy()
        self._cards.clear()
        self._columns = 0
        self._build_header()
        self._build_filters()
        self._build_viewport()
        self.query_var.set(self.state_model.query)
        self.refresh()


NexusControlCenter = NexusLabHub
FeatureHub = NexusLabHub


__all__ = [
    "FEATURES", "FeatureHub", "FeatureSpec", "FeatureStatus", "LAB_GROUPS",
    "LAB_GROUP_LABELS", "LabNavigationState", "NexusControlCenter",
    "NexusLabHub", "STATUS_STATES", "feature_by_key", "feature_rows",
    "filter_features", "group_counts", "normalize_feature_key",
    "normalize_status", "normalize_statuses", "responsive_columns",
    "status_badge",
]
