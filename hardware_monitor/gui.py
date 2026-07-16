from __future__ import annotations

import math
import platform
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
import weakref
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from hardware_monitor.monitor import (
    cpu_self_test,
    disk_self_test,
    hardware_info,
    take_snapshot,
)
from hardware_monitor.network import (
    NetworkRateTracker,
    NetworkRates,
    format_bytes,
    format_link_speed,
    format_rate,
)
from hardware_monitor.recorder import SessionRecorder


BG = "#030304"
PANEL = "#09090b"
CARD = "#111113"
CARD_2 = "#18181b"
BORDER = "#35353b"
BORDER_STRONG = "#5a5a62"
TEXT = "#f5f5f7"
MUTED = "#9a9aa2"
CYAN = "#f5f5f7"
GREEN = "#d7d7db"
PURPLE = "#ff6475"
ORANGE = "#ff8b98"
RED = "#f23d52"
RED_DARK = "#68131f"
TRACK = "#252529"
GRID = "#242428"
GRAPH_FILL = "#28090f"

FONT_UI = "Segoe UI"
FONT_DISPLAY = "Segoe UI"
FONT_MONO = "Cascadia Mono"

GITHUB_URL = "https://github.com/Kieranmcm07"


def value_text(value: float | None, suffix: str = "", decimals: int = 1) -> str:
    return "N/A" if value is None else f"{value:.{decimals}f}{suffix}"


def uptime_text(seconds: float | None) -> str:
    if seconds is None:
        return "Unavailable"
    total = int(seconds)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def duration_text(seconds: float | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def compact_volume_name(name: str, limit: int = 24) -> str:
    """Keep long mount paths readable without hiding the capacity percentage."""
    if len(name) <= limit:
        return name
    left = (limit - 1) // 2
    right = limit - left - 1
    return f"{name[:left]}…{name[-right:]}"


def configure_font_families(root: tk.Misc) -> None:
    """Choose modern installed fonts while keeping Linux fallbacks predictable."""
    global FONT_UI, FONT_DISPLAY, FONT_MONO
    installed = {family.casefold(): family for family in tkfont.families(root)}

    def choose(*candidates: str) -> str:
        for candidate in candidates:
            if candidate.casefold() in installed:
                return installed[candidate.casefold()]
        return candidates[-1]

    FONT_UI = choose(
        "Segoe UI Variable Text", "Bahnschrift", "Inter", "Noto Sans", "DejaVu Sans"
    )
    FONT_DISPLAY = choose(
        "Segoe UI Variable Display", "Bahnschrift", FONT_UI, "DejaVu Sans"
    )
    FONT_MONO = choose(
        "Cascadia Mono", "JetBrains Mono", "Consolas", "DejaVu Sans Mono"
    )


def mix_color(first: str, second: str, amount: float) -> str:
    """Blend two #RRGGBB colours for short, low-cost UI transitions."""
    amount = max(0.0, min(1.0, amount))
    start = tuple(int(first[index:index + 2], 16) for index in (1, 3, 5))
    end = tuple(int(second[index:index + 2], 16) for index in (1, 3, 5))
    values = tuple(round(a + (b - a) * amount) for a, b in zip(start, end))
    return "#" + "".join(f"{value:02x}" for value in values)


def register_animated(widget: tk.Widget) -> None:
    register = getattr(widget.winfo_toplevel(), "_register_animated", None)
    if register is not None:
        register(widget)


def rounded_rectangle(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float = 16,
    **options,
) -> int:
    """Draw a smooth rounded panel using only standard Tk canvas primitives."""
    radius = max(2.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = (
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    )
    return canvas.create_polygon(
        points, smooth=True, splinesteps=32, **options
    )


class RoundedPanel(tk.Frame):
    """A normal Tk container with a Canvas-drawn rounded surface behind it."""

    def __init__(
        self,
        parent,
        *,
        surface: str = CARD,
        outline: str = BORDER,
        radius: int = 18,
        border_width: int = 2,
        padding: int = 3,
    ):
        try:
            parent_bg = getattr(parent, "surface_color", str(parent.cget("bg")))
        except tk.TclError:
            parent_bg = BG
        super().__init__(
            parent, bg=parent_bg, bd=0, highlightthickness=0,
            padx=padding, pady=padding
        )
        self.surface_color = surface
        self.outline_color = outline
        self.radius = radius
        self.border_width = border_width
        self.surface = tk.Canvas(
            self, bg=parent_bg, bd=0, highlightthickness=0
        )
        self.surface.place(x=0, y=0, relwidth=1, relheight=1)
        self.bind("<Configure>", self._draw_surface, add="+")

    def set_outline(self, color: str) -> None:
        self.outline_color = color
        self._draw_surface()

    def _draw_surface(self, _event=None) -> None:
        self.surface.delete("all")
        width = max(8, self.winfo_width())
        height = max(8, self.winfo_height())
        inset = max(1, self.border_width / 2)
        rounded_rectangle(
            self.surface, inset, inset, width - inset, height - inset,
            self.radius, fill=self.surface_color, outline=self.outline_color,
            width=self.border_width
        )


class RoundedButton(tk.Canvas):
    """Keyboard-accessible rounded button with a short hover/press transition."""

    def __init__(
        self,
        parent,
        *,
        text: str,
        command,
        bg: str = CARD_2,
        fg: str = TEXT,
        activebackground: str = BORDER,
        activeforeground: str = TEXT,
        padx: int = 12,
        pady: int = 7,
        font=None,
        cursor: str = "hand2",
        radius: int = 12,
        **_ignored,
    ):
        try:
            parent_bg = getattr(parent, "surface_color", str(parent.cget("bg")))
        except tk.TclError:
            parent_bg = BG
        self._text = text
        self._command = command
        self._normal_fill = bg
        self._normal_text = fg
        self._hover_fill = activebackground
        self._hover_text = activeforeground
        self._font = font or (FONT_UI, 9, "bold")
        self._padx = padx
        self._pady = pady
        self._radius = radius
        self._hover = 0.0
        self._hover_target = 0.0
        self._pressed = False
        self._selected = False
        self._focused = False
        self._state = "normal"
        measured = tkfont.Font(root=parent, font=self._font)
        width = measured.measure(text) + padx * 2 + 4
        height = measured.metrics("linespace") + pady * 2 + 4
        super().__init__(
            parent, width=width, height=height, bg=parent_bg, bd=0,
            highlightthickness=0, cursor=cursor, takefocus=1
        )
        self.bind("<Configure>", lambda _event: self.draw())
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda _event: self._set_focus(True))
        self.bind("<FocusOut>", lambda _event: self._set_focus(False))
        self.bind("<Return>", self._keyboard_activate)
        self.bind("<space>", self._keyboard_activate)
        register_animated(self)
        self.draw()

    def _set_focus(self, focused: bool) -> None:
        self._focused = focused
        self.draw()

    def _set_hover(self, hovering: bool) -> None:
        if self._state == "disabled":
            return
        self._hover_target = 1.0 if hovering else 0.0

    def _leave(self, _event=None) -> None:
        self._pressed = False
        self._set_hover(False)

    def _press(self, _event=None) -> None:
        if self._state != "disabled":
            self._pressed = True
            self.focus_set()
            self.draw()

    def _release(self, event=None) -> None:
        if self._state == "disabled":
            return
        was_pressed = self._pressed
        self._pressed = False
        self.draw()
        inside = event is None or (
            0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        )
        if was_pressed and inside and self._command:
            self._command()

    def _keyboard_activate(self, _event=None) -> str:
        if self._state != "disabled" and self._command:
            self._command()
        return "break"

    def animate_frame(self, _now: float) -> None:
        difference = self._hover_target - self._hover
        if abs(difference) < 0.02:
            self._hover = self._hover_target
            return
        self._hover += difference * 0.38
        self.draw()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.draw()

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        if not hasattr(self, "_normal_fill"):
            return super().configure(**kwargs)
        redraw = False
        resize = False
        for key in tuple(kwargs):
            value = kwargs.pop(key)
            if key == "text":
                self._text = str(value)
                redraw = resize = True
            elif key in {"bg", "background"}:
                self._normal_fill = str(value)
                redraw = True
            elif key in {"fg", "foreground"}:
                self._normal_text = str(value)
                redraw = True
            elif key == "activebackground":
                self._hover_fill = str(value)
                redraw = True
            elif key == "activeforeground":
                self._hover_text = str(value)
                redraw = True
            elif key == "state":
                self._state = str(value)
                self._hover_target = 0.0
                self._hover = 0.0
                redraw = True
            else:
                super().configure(**{key: value})
        if resize:
            measured = tkfont.Font(root=self, font=self._font)
            super().configure(
                width=measured.measure(self._text) + self._padx * 2 + 4,
                height=measured.metrics("linespace") + self._pady * 2 + 4,
            )
        if redraw:
            self.draw()

    config = configure

    def draw(self) -> None:
        self.delete("all")
        width = max(20, self.winfo_width())
        height = max(18, self.winfo_height())
        hover = min(1.0, self._hover + (0.18 if self._pressed else 0.0))
        fill = mix_color(self._normal_fill, self._hover_fill, hover)
        foreground = mix_color(self._normal_text, self._hover_text, hover)
        if self._state == "disabled":
            fill = mix_color(self._normal_fill, BG, 0.35)
            foreground = mix_color(MUTED, BG, 0.25)
        outline = RED if self._selected else BORDER_STRONG if self._focused else BORDER
        rounded_rectangle(
            self, 2, 2, width - 2, height - 2, self._radius,
            fill=fill, outline=outline, width=2
        )
        if self._selected:
            self.create_line(
                self._radius, height - 4, width - self._radius, height - 4,
                fill=RED, width=3, capstyle="round"
            )
        self.create_text(
            width / 2, height / 2, text=self._text, fill=foreground,
            font=self._font
        )


class Gauge(tk.Canvas):
    def __init__(self, parent, title: str, color: str, size: int = 145):
        super().__init__(parent, width=size, height=size, bg=CARD, highlightthickness=0)
        self.size = size
        self.title = title
        self.color = color
        self.value: float | None = None
        self.display_value = 0.0
        self._end_point: tuple[float, float] | None = None
        self.bind("<Configure>", lambda _event: self.draw())
        register_animated(self)

    def set(self, value: float | None) -> None:
        self.value = None if value is None else max(0.0, min(100.0, float(value)))
        if self.value is None:
            self.display_value = 0.0
            self.draw()

    def draw(self) -> None:
        self.delete("all")
        s, pad = self.size, 14
        rounded_rectangle(
            self, 2, 2, s - 2, s - 2, 22,
            fill=CARD_2, outline=BORDER, width=2
        )
        for width, shade in ((22, TRACK), (17, "#2d2d32"), (12, "#34343a")):
            self.create_arc(pad, pad, s - pad, s - pad, start=225, extent=-270,
                            style="arc", width=width, outline=shade)
        center = s / 2
        radius = (s - 2 * pad) / 2
        for index in range(28):
            angle = math.radians(225 - 270 * index / 27)
            outer = radius + 4
            inner = radius - (3 if index % 3 else 6)
            self.create_line(center + inner * math.cos(angle), center - inner * math.sin(angle),
                             center + outer * math.cos(angle), center - outer * math.sin(angle),
                             fill=BORDER_STRONG, width=2 if index % 3 == 0 else 1)
        if self.value is not None:
            self.create_arc(pad, pad, s - pad, s - pad, start=225,
                            extent=-270 * self.display_value / 100, style="arc", width=12,
                            outline=self.color)
            angle = math.radians(225 - 270 * self.display_value / 100)
            self._end_point = (
                center + radius * math.cos(angle),
                center - radius * math.sin(angle),
            )
            x, y = self._end_point
            self.create_oval(
                x - 5, y - 5, x + 5, y + 5,
                fill=CARD_2, outline=self.color, width=2, tags="gauge_halo"
            )
            reading = f"{self.value:.0f}%"
            state = "HIGH" if self.value >= 85 else "ELEVATED" if self.value >= 65 else "NORMAL"
            state_color = RED if self.value >= 85 else ORANGE if self.value >= 65 else GREEN
        else:
            self._end_point = None
            reading = "N/A"
            state = "NO SENSOR"
            state_color = MUTED
        self.create_text(s / 2, s / 2 - 5, text=reading, fill=TEXT,
                         font=(FONT_DISPLAY, 24, "bold"))
        self.create_text(s / 2, s / 2 + 23, text=self.title, fill=MUTED,
                         font=(FONT_UI, 9, "bold"))
        self.create_text(s / 2, s / 2 + 41, text=state, fill=state_color,
                         font=(FONT_UI, 8, "bold"))

    def animate_frame(self, now: float) -> None:
        if self.value is not None:
            difference = self.value - self.display_value
            if abs(difference) >= 0.25:
                self.display_value += difference * 0.30
                self.draw()
            elif self.display_value != self.value:
                self.display_value = self.value
                self.draw()
        if self._end_point is None:
            return
        pulse = (math.sin(now * 4.2) + 1.0) / 2.0
        radius = 5 + pulse * 3
        x, y = self._end_point
        self.coords("gauge_halo", x - radius, y - radius, x + radius, y + radius)
        self.itemconfigure(
            "gauge_halo", outline=mix_color(self.color, TEXT, pulse * 0.35)
        )


class HistoryGraph(tk.Canvas):
    def __init__(self, parent, title: str, color: str):
        super().__init__(parent, bg=BG, height=190, highlightthickness=0)
        self.title = title
        self.color = color
        self.values: deque[float | None] = deque(maxlen=60)
        self._plot_bounds: tuple[float, float, float, float] | None = None
        self.bind("<Configure>", lambda _event: self.draw())
        register_animated(self)

    def add(self, value: float | None) -> None:
        # Keep an empty slot when a sensor is unavailable so a gap is visible
        # instead of making old and new readings look adjacent in time.
        self.values.append(None if value is None else float(value))
        self.draw()

    def clear(self) -> None:
        self.values.clear()
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 120)
        height = max(self.winfo_height(), 110)
        rounded_rectangle(
            self, 2, 2, width - 2, height - 2, 18,
            fill=CARD, outline=BORDER, width=2
        )
        available = [value for value in self.values if value is not None]
        if available:
            current = self.values[-1]
            now = "N/A" if current is None else f"{current:.1f}%"
            average = sum(available) / len(available)
            peak = max(available)
            stats = f"NOW {now}    AVG {average:.1f}%    PEAK {peak:.1f}%"
        else:
            stats = "WAITING FOR SENSOR DATA"
        self.create_text(18, 17, anchor="w", text=self.title, fill=TEXT,
                         font=(FONT_DISPLAY, 11, "bold"))
        if width < 680:
            self.create_text(18, 38, anchor="w", text=stats, fill=self.color,
                             font=(FONT_UI, 8, "bold"))
            top = 61
        else:
            self.create_text(width - 18, 17, anchor="e", text=stats, fill=self.color,
                             font=(FONT_UI, 9, "bold"))
            top = 48
        bottom = height - 22
        for percent in (0, 25, 50, 75, 100):
            y = bottom - (bottom - top) * percent / 100
            self.create_line(42, y, width - 16, y, fill=GRID, width=1)
            if percent in (0, 50, 100):
                self.create_text(34, y, anchor="e", text=str(percent), fill=MUTED,
                                 font=(FONT_UI, 8))
        if not available:
            self._plot_bounds = None
            return
        values = list(self.values)
        segments: list[list[float]] = []
        points: list[float] = []
        for index, value in enumerate(values):
            if value is None:
                if points:
                    segments.append(points)
                    points = []
                continue
            slot = 60 - len(values) + index
            x = 42 + (width - 58) * slot / 59
            y = bottom - (bottom - top) * max(0, min(100, value)) / 100
            points.extend((x, y))
        if points:
            segments.append(points)
        for segment in segments:
            if len(segment) >= 4:
                self.create_polygon(
                    [segment[0], bottom] + segment + [segment[-2], bottom],
                    fill=GRAPH_FILL, outline="", stipple="gray50"
                )
                self.create_line(
                    segment, fill=self.color, width=3, smooth=True,
                    splinesteps=12, capstyle="round", joinstyle="round"
                )
            else:
                x, y = segment
                self.create_oval(x - 2, y - 2, x + 2, y + 2,
                                 fill=self.color, outline="")
        if self.values[-1] is not None:
            last_segment = segments[-1]
            x, y = last_segment[-2:]
            self.create_oval(x - 5, y - 5, x + 5, y + 5,
                             outline=self.color, width=3)
        self.create_text(42, height - 8, anchor="w", text="-60s", fill=MUTED,
                         font=(FONT_UI, 8))
        self.create_text(width / 2, height - 8, text="-30s", fill=MUTED,
                         font=(FONT_UI, 8))
        self.create_text(width - 16, height - 8, anchor="e", text="NOW", fill=MUTED,
                         font=(FONT_UI, 8))
        self._plot_bounds = (42, top, width - 16, bottom)
        self.create_line(42, top, 42, bottom, fill=RED_DARK, width=2, tags="sweep")

    def animate_frame(self, now: float) -> None:
        if self._plot_bounds is None:
            return
        left, top, right, bottom = self._plot_bounds
        phase = (now * 0.18) % 1.0
        x = left + (right - left) * phase
        self.coords("sweep", x, top, x, bottom)
        self.itemconfigure("sweep", fill=mix_color(RED_DARK, RED, 0.2 + phase * 0.25))


class RateHistoryGraph(tk.Canvas):
    """Sixty-sample network graph with a scale that follows recent traffic."""

    def __init__(self, parent, title: str, color: str):
        super().__init__(parent, bg=BG, height=175, highlightthickness=0)
        self.title = title
        self.color = color
        self.values: deque[float | None] = deque(maxlen=60)
        self._plot_bounds: tuple[float, float, float, float] | None = None
        self.bind("<Configure>", lambda _event: self.draw())
        register_animated(self)

    def add(self, value: float | None) -> None:
        self.values.append(None if value is None else max(0.0, float(value)))
        self.draw()

    def clear(self) -> None:
        self.values.clear()
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 160)
        height = max(self.winfo_height(), 110)
        rounded_rectangle(
            self, 2, 2, width - 2, height - 2, 18,
            fill=CARD, outline=BORDER, width=2
        )
        available = [value for value in self.values if value is not None]
        current = self.values[-1] if self.values else None
        peak = max(available, default=0.0)
        scale = max(1024.0, peak * 1.15)
        if available:
            now = "N/A" if current is None else format_rate(current)
            stats = f"NOW {now}    PEAK {format_rate(peak)}"
        else:
            stats = "WAITING FOR NETWORK DATA"
        self.create_text(18, 17, anchor="w", text=self.title, fill=TEXT,
                         font=(FONT_DISPLAY, 10, "bold"))
        if width < 620:
            self.create_text(18, 37, anchor="w", text=stats, fill=self.color,
                             font=(FONT_UI, 8, "bold"))
            top = 58
        else:
            self.create_text(width - 18, 17, anchor="e", text=stats, fill=self.color,
                             font=(FONT_UI, 8, "bold"))
            top = 46
        bottom = height - 22
        for fraction in (0.0, 0.5, 1.0):
            y = bottom - (bottom - top) * fraction
            self.create_line(72, y, width - 16, y, fill=GRID, width=1)
            self.create_text(
                64, y, anchor="e", text=format_rate(scale * fraction),
                fill=MUTED, font=(FONT_UI, 8)
            )
        segments: list[list[float]] = []
        points: list[float] = []
        values = list(self.values)
        for index, value in enumerate(values):
            if value is None:
                if points:
                    segments.append(points)
                    points = []
                continue
            slot = 60 - len(values) + index
            x = 72 + (width - 88) * slot / 59
            y = bottom - (bottom - top) * min(value, scale) / scale
            points.extend((x, y))
        if points:
            segments.append(points)
        for segment in segments:
            if len(segment) >= 4:
                self.create_polygon(
                    [segment[0], bottom] + segment + [segment[-2], bottom],
                    fill=GRAPH_FILL, outline="", stipple="gray50"
                )
                self.create_line(
                    segment, fill=self.color, width=3, smooth=True,
                    splinesteps=12, capstyle="round", joinstyle="round"
                )
            else:
                x, y = segment
                self.create_oval(x - 2, y - 2, x + 2, y + 2,
                                 fill=self.color, outline="")
        if current is not None and segments:
            x, y = segments[-1][-2:]
            self.create_oval(x - 5, y - 5, x + 5, y + 5,
                             outline=self.color, width=3)
        self.create_text(72, height - 8, anchor="w", text="-60s", fill=MUTED,
                         font=(FONT_UI, 8))
        self.create_text(width - 16, height - 8, anchor="e", text="NOW", fill=MUTED,
                         font=(FONT_UI, 8))
        self._plot_bounds = (72, top, width - 16, bottom)
        if available:
            self.create_line(72, top, 72, bottom, fill=RED_DARK, width=2, tags="sweep")

    def animate_frame(self, now: float) -> None:
        if self._plot_bounds is None or not self.values:
            return
        left, top, right, bottom = self._plot_bounds
        phase = (now * 0.18) % 1.0
        x = left + (right - left) * phase
        self.coords("sweep", x, top, x, bottom)
        self.itemconfigure("sweep", fill=mix_color(RED_DARK, RED, 0.2 + phase * 0.25))


class MiniSparkline(tk.Canvas):
    def __init__(self, parent, color: str):
        super().__init__(parent, bg=CARD_2, height=55, highlightthickness=0)
        self.color = color
        self.values: deque[float | None] = deque(maxlen=40)
        self._last_point: tuple[float, float] | None = None
        self.bind("<Configure>", lambda _e: self.draw())
        register_animated(self)

    def add(self, value: float | None) -> None:
        self.values.append(None if value is None else float(value))
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        width, height = max(80, self.winfo_width()), max(35, self.winfo_height())
        rounded_rectangle(
            self, 1, 1, width - 1, height - 1, 11,
            fill=CARD, outline=BORDER, width=2
        )
        if not any(value is not None for value in self.values):
            self._last_point = None
            self.create_text(width / 2, height / 2, text="WAITING", fill=MUTED,
                             font=(FONT_UI, 8, "bold"))
            return
        values = list(self.values)
        segments: list[list[float]] = []
        points: list[float] = []
        for index, value in enumerate(values):
            if value is None:
                if points:
                    segments.append(points)
                    points = []
                continue
            x = 4 + (width - 8) * index / max(1, len(values) - 1)
            y = height - 5 - (height - 10) * min(100, max(0, value)) / 100
            points.extend((x, y))
        if points:
            segments.append(points)
        for segment in segments:
            if len(segment) >= 4:
                self.create_line(
                    segment, fill=self.color, width=3, smooth=True,
                    splinesteps=10, capstyle="round", joinstyle="round"
                )
            else:
                x, y = segment
                self.create_oval(x - 2, y - 2, x + 2, y + 2,
                                 fill=self.color, outline="")
        if self.values[-1] is not None and segments:
            x, y = segments[-1][-2:]
            self._last_point = (x, y)
            self.create_oval(
                x - 4, y - 4, x + 4, y + 4,
                fill=CARD, outline=self.color, width=2, tags="spark_halo"
            )
        else:
            self._last_point = None

    def animate_frame(self, now: float) -> None:
        if self._last_point is None:
            return
        pulse = (math.sin(now * 4.4) + 1.0) / 2.0
        radius = 4 + pulse * 2
        x, y = self._last_point
        self.coords("spark_halo", x - radius, y - radius, x + radius, y + radius)
        self.itemconfigure(
            "spark_halo", outline=mix_color(self.color, TEXT, pulse * 0.35)
        )


class NeonScanline(tk.Canvas):
    def __init__(self, parent):
        super().__init__(parent, bg=PANEL, height=5, highlightthickness=0)
        self._burst_until = 0.0
        register_animated(self)

    def burst(self) -> None:
        self._burst_until = time.monotonic() + 0.9

    def animate_frame(self, now: float) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        speed = 0.55 if now < self._burst_until else 0.20
        position = (now * speed * width) % (width + 250)
        x = position - 250
        self.create_line(0, 2, width, 2, fill=GRID, width=1)
        self.create_line(x, 2, x + 250, 2, fill="#301017", width=2)
        self.create_line(x + 90, 2, x + 250, 2, fill=RED_DARK, width=3)
        self.create_line(x + 185, 2, x + 250, 2, fill=RED, width=3)


class MetricTile(tk.Canvas):
    def __init__(self, parent, label: str, color: str):
        parent_bg = getattr(parent, "surface_color", str(parent.cget("bg")))
        super().__init__(
            parent, bg=parent_bg, width=120, height=94, highlightthickness=0,
            borderwidth=0
        )
        self.label_text = label
        self.color = color
        self.value_text = "N/A"
        self.detail_text = "Waiting for data"
        self.surface = CARD if parent_bg.lower() == CARD_2.lower() else CARD_2
        self._panel_id: int | None = None
        self._rail_id: int | None = None
        self._dot_id: int | None = None
        self._hover = 0.0
        self._hover_target = 0.0
        self._flash_until = 0.0
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<Configure>", lambda _event: self.draw())
        register_animated(self)

    def _set_hover(self, hovering: bool) -> None:
        self._hover_target = 1.0 if hovering else 0.0

    def set(self, value: str, detail: str) -> None:
        if self.value_text not in {"N/A", value}:
            self._flash_until = time.monotonic() + 0.42
        self.value_text = value
        self.detail_text = detail
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        width = max(110, self.winfo_width())
        height = max(90, self.winfo_height())
        self._panel_id = rounded_rectangle(
            self, 2, 2, width - 2, height - 2, 15,
            fill=self.surface, outline=BORDER, width=2
        )
        self.create_line(
            20, 7, width - 20, 7, fill=mix_color(BORDER, TEXT, 0.16), width=1
        )
        self._rail_id = rounded_rectangle(
            self, 12, 12, 18, 33, 3,
            fill=self.color, outline=self.color
        )
        self._dot_id = self.create_oval(
            width - 24, 15, width - 16, 23,
            fill=self.surface, outline=self.color, width=2
        )
        self.create_text(
            25, 21, anchor="w", text=self.label_text, fill=self.color,
            font=(FONT_UI, 8, "bold")
        )
        self.create_text(
            14, 50, anchor="w", text=self.value_text, fill=TEXT,
            font=(FONT_DISPLAY, 17, "bold")
        )
        self.create_text(
            14, 70, anchor="nw", width=max(80, width - 28),
            text=self.detail_text, fill=MUTED, font=(FONT_UI, 8)
        )

    def animate_frame(self, now: float) -> None:
        self._hover += (self._hover_target - self._hover) * 0.22
        flash = max(0.0, min(1.0, (self._flash_until - now) / 0.42))
        intensity = max(self._hover * 0.65, flash)
        if self._panel_id is not None:
            self.itemconfigure(
                self._panel_id,
                outline=mix_color(BORDER, self.color, intensity * 0.78),
                width=3 if intensity > 0.35 else 2,
            )
        if self._dot_id is not None:
            self.itemconfigure(
                self._dot_id,
                fill=mix_color(self.surface, self.color, intensity * 0.55),
                outline=mix_color(self.color, TEXT, intensity * 0.28),
            )


class DriveCard(RoundedPanel):
    """Responsive storage card backed by operating-system volume data."""

    def __init__(self, parent, name: str):
        super().__init__(
            parent, surface=CARD, outline=BORDER, radius=20,
            border_width=2, padding=6
        )
        self.name = name
        self.percent = 0.0
        self.display_percent = 0.0
        self.color = GREEN
        rail = tk.Frame(self, bg=GREEN, width=6)
        rail.pack(side="left", fill="y", padx=(8, 0), pady=10)
        self.rail = rail
        body = tk.Frame(self, bg=CARD)
        body.pack(side="left", fill="both", expand=True, padx=16, pady=13)
        heading = tk.Frame(body, bg=CARD)
        heading.pack(fill="x")
        heading.columnconfigure(0, weight=1)
        self.drive_label = tk.Label(heading, text=name, bg=CARD, fg=TEXT,
                                    anchor="w", font=(FONT_MONO, 16, "bold"))
        self.drive_label.grid(row=0, column=0, sticky="ew")
        self.system_badge = tk.Label(heading, text="", bg=CARD, fg=CYAN,
                                     font=(FONT_UI, 8, "bold"))
        self.system_badge.grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.percent_label = tk.Label(heading, text="0.0% USED", bg=CARD, fg=GREEN,
                                      font=(FONT_MONO, 12, "bold"))
        self.percent_label.grid(row=0, column=1, sticky="e", padx=(10, 0))
        self.capacity_label = tk.Label(body, text="Waiting for volume data", bg=CARD,
                                       fg=MUTED, anchor="w", justify="left",
                                       font=(FONT_UI, 9))
        self.capacity_label.pack(anchor="w", pady=(4, 9))
        body.bind(
            "<Configure>",
            lambda event: self.capacity_label.configure(
                wraplength=max(160, event.width - 4)
            ),
        )
        self.bar = tk.Canvas(body, bg=CARD, height=22, highlightthickness=0)
        self.bar.pack(fill="x")
        self.bar.bind("<Configure>", lambda _event: self._draw_bar())
        self.state_label = tk.Label(body, text="NORMAL CAPACITY", bg=CARD, fg=GREEN,
                                    font=(FONT_UI, 8, "bold"))
        self.state_label.pack(anchor="e", pady=(5, 0))
        register_animated(self)

    def set(self, drive, is_system: bool = False) -> None:
        self.name = drive.name
        self.percent = max(0.0, min(100.0, float(drive.used_percent)))
        self.color = RED if self.percent >= 90 else ORANGE if self.percent >= 75 else GREEN
        state = "CAPACITY CRITICAL" if self.percent >= 90 else "CAPACITY ELEVATED" if self.percent >= 75 else "NORMAL CAPACITY"
        display_name = compact_volume_name(drive.name)
        self.drive_label.configure(text=display_name)
        self.system_badge.configure(
            text="SYSTEM VOLUME" if is_system else "LOCAL VOLUME"
        )
        self.percent_label.configure(text=f"{self.percent:.1f}% USED", fg=self.color)
        capacity = (
            f"{drive.free_gib:.1f} GiB free  /  {drive.total_gib:.1f} GiB total"
        )
        detail = f"{drive.name}\n{capacity}" if display_name != drive.name else capacity
        self.capacity_label.configure(text=detail)
        self.state_label.configure(text=state, fg=self.color)
        self.rail.configure(bg=self.color)
        self.set_outline(self.color)

    def animate_frame(self, _now: float) -> None:
        difference = self.percent - self.display_percent
        if abs(difference) < 0.15:
            if self.display_percent != self.percent:
                self.display_percent = self.percent
                self._draw_bar()
            return
        self.display_percent += difference * 0.32
        self._draw_bar()

    def _draw_bar(self) -> None:
        self.bar.delete("all")
        width = max(20, self.bar.winfo_width())
        height = max(12, self.bar.winfo_height())
        rounded_rectangle(
            self.bar, 0, 3, width, height - 3, 8,
            fill=TRACK, outline=BORDER, width=2
        )
        fill_width = width * self.display_percent / 100
        if fill_width > 2:
            rounded_rectangle(
                self.bar, 1, 4, max(3, fill_width), height - 4, 7,
                fill=self.color, outline=""
            )
        for marker in (25, 50, 75):
            x = width * marker / 100
            self.bar.create_line(x, 5, x, height - 5, fill=BORDER_STRONG, width=2)


class NetworkAdapterCard(RoundedPanel):
    """Live rate and negotiated-link summary for one connected adapter."""

    def __init__(self, parent, luid: int):
        super().__init__(
            parent, surface=CARD_2, outline=BORDER, radius=18,
            border_width=2, padding=6
        )
        self.luid = luid
        self.alias = tk.Label(self, text="NETWORK ADAPTER", bg=CARD_2, fg=TEXT,
                              font=(FONT_DISPLAY, 11, "bold"))
        self.alias.pack(anchor="w", padx=13, pady=(10, 0))
        self.description = tk.Label(
            self, text="Waiting for system data", bg=CARD_2, fg=MUTED,
            anchor="w", justify="left", wraplength=390, font=(FONT_UI, 9)
        )
        self.description.pack(fill="x", padx=13, pady=(1, 7))
        row = tk.Frame(self, bg=CARD_2)
        row.pack(fill="x", padx=13, pady=(0, 10))
        self.kind = tk.Label(row, text="NETWORK", bg=CARD, fg=CYAN,
                             font=(FONT_UI, 8, "bold"), padx=7, pady=3)
        self.kind.pack(side="left")
        self.link = tk.Label(row, text="LINK --", bg=CARD_2, fg=MUTED,
                             font=(FONT_MONO, 8, "bold"))
        self.link.pack(side="left", padx=9)
        self.upload = tk.Label(row, text="UP 0 B/s", bg=CARD_2, fg=PURPLE,
                               font=(FONT_MONO, 8, "bold"))
        self.upload.pack(side="right")
        self.download = tk.Label(row, text="DOWN 0 B/s", bg=CARD_2, fg=CYAN,
                                 font=(FONT_MONO, 8, "bold"))
        self.download.pack(side="right", padx=(0, 13))

    def set(self, adapter) -> None:
        accent = PURPLE if adapter.kind == "Wi-Fi" else GREEN if adapter.kind == "Mobile broadband" else CYAN
        self.alias.configure(text=adapter.alias)
        self.description.configure(text=adapter.description)
        self.kind.configure(text=adapter.kind.upper(), fg=accent)
        self.set_outline(accent)
        receive_link = format_link_speed(adapter.receive_link_bps)
        transmit_link = format_link_speed(adapter.transmit_link_bps)
        link = receive_link if receive_link == transmit_link else f"{receive_link} down / {transmit_link} up"
        self.link.configure(text=f"LINK {link}")
        self.download.configure(text=f"DOWN {format_rate(adapter.download_bps)}")
        self.upload.configure(text=f"UP {format_rate(adapter.upload_bps)}")


class HardwareDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        configure_font_families(self)
        self.title("NEXUS - Hardware Monitor")
        self.geometry("1100x760")
        self.minsize(860, 640)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close)
        # Telemetry is "latest value" data. Keeping one pending snapshot avoids
        # replaying an old backlog after a modal dialog or a busy resize.
        self.snapshot_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        self.results: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=16)
        self.stop_event = threading.Event()
        self.session = SessionRecorder()
        self.network_tracker = NetworkRateTracker()
        self.graphs_paused = False
        self.compact = False
        self.hud_borderless = False
        self.normal_geometry = "1100x760"
        self.normal_state = "normal"
        self.latest_snapshot = None
        self.latest_network: NetworkRates | None = None
        self.sensor_state = "starting"
        self.hardware = hardware_info()
        self._animated_widgets: weakref.WeakSet[tk.Widget] = weakref.WeakSet()
        self._animation_job: str | None = None
        self._drag_origin = (0, 0)
        self._configure_style()
        self._build_header()
        self._build_content()
        self._build_footer()
        self.after(50, self._enable_dark_titlebar)
        self.after(100, self._poll_results)
        self.after(250, self._tick_clock)
        self._animation_job = self.after(100, self._animation_tick)
        self.bind("<Escape>", lambda _event: self.exit_hud() if self.compact else None)
        threading.Thread(target=self._sampler, daemon=True).start()

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Nexus.TNotebook", background=BG, borderwidth=0)
        style.layout("Nexus.TNotebook.Tab", [])
        style.configure(
            "Vertical.TScrollbar", background=CARD_2, troughcolor=BG,
            bordercolor=BG, arrowcolor=TEXT, lightcolor=CARD_2,
            darkcolor=CARD_2, relief="flat", borderwidth=0, width=12
        )
        style.map(
            "Vertical.TScrollbar",
            background=[("active", RED), ("pressed", RED)],
        )

    def _enable_dark_titlebar(self) -> None:
        if platform.system() != "Windows":
            return
        try:
            import ctypes
            enabled = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.winfo_id(), 20, ctypes.byref(enabled), ctypes.sizeof(enabled)
            )
        except Exception:
            pass

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=PANEL, height=68)
        self.header = header
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="NEXUS", bg=PANEL, fg=TEXT,
                 font=(FONT_DISPLAY, 21, "bold")).pack(side="left", padx=(22, 6))
        tk.Label(header, text="// HARDWARE MONITOR", bg=PANEL, fg=RED,
                 font=(FONT_UI, 9, "bold")).pack(side="left", pady=(6, 0))
        self.live_status = tk.Label(header, text="STARTING", bg=PANEL, fg=ORANGE,
                                    font=(FONT_UI, 9, "bold"))
        self.live_status.pack(side="right", padx=20)
        self.live_orb = tk.Canvas(header, width=20, height=20, bg=PANEL, highlightthickness=0)
        self.live_orb.pack(side="right")
        self.clock_label = tk.Label(header, text="--:--:--", bg=PANEL, fg=TEXT,
                                    font=(FONT_MONO, 10, "bold"))
        self.clock_label.pack(side="right", padx=18)
        self.compact_button = RoundedButton(
            header, text="COMPACT MODE", command=self.toggle_compact, bg=CARD,
            fg=TEXT, activebackground=BORDER, activeforeground=TEXT, relief="flat",
            cursor="hand2", padx=13, pady=7, font=(FONT_UI, 8, "bold")
        )
        self.compact_button.pack(side="right")
        self.scanline = NeonScanline(self)
        self.scanline.pack(fill="x")

    def _tick_clock(self) -> None:
        if self.stop_event.is_set():
            return
        self.clock_label.configure(text=datetime.now().strftime("%H:%M:%S"))
        self.after(1000, self._tick_clock)

    def _register_animated(self, widget: tk.Widget) -> None:
        self._animated_widgets.add(widget)

    def _animation_tick(self) -> None:
        if self.stop_event.is_set():
            return
        now = time.monotonic()
        try:
            visible = self.state() not in {"iconic", "withdrawn"}
        except tk.TclError:
            return
        if visible:
            for widget in list(self._animated_widgets):
                try:
                    if widget.winfo_exists() and widget.winfo_viewable():
                        widget.animate_frame(now)
                except tk.TclError:
                    continue
            self._draw_live_indicator(now)
            if hasattr(self, "hero_status") and self.hero_status.cget("text") == "ATTENTION":
                pulse = (math.sin(now * 5.0) + 1.0) / 2.0
                self.hero_status.configure(fg=mix_color(RED, ORANGE, pulse * 0.32))
        self._animation_job = self.after(40, self._animation_tick)

    def _draw_live_indicator(self, now: float) -> None:
        if not self.live_orb.winfo_viewable():
            return
        pulse = (math.sin(now * 4.2) + 1.0) / 2.0
        radius = 7 + pulse * 2.5
        self.live_orb.delete("all")
        if self.sensor_state == "live":
            color, outline = TEXT, RED
        elif self.sensor_state == "error":
            color, outline = RED, ORANGE
        else:
            color, outline = MUTED, RED_DARK
        self.live_orb.create_oval(
            10 - radius, 10 - radius, 10 + radius, 10 + radius,
            fill="", outline=mix_color(RED_DARK, outline, 1.0 - pulse), width=2
        )
        self.live_orb.create_oval(6, 6, 14, 14, fill=color, outline=outline, width=2)
        if hasattr(self, "recording_orb") and self.session.active:
            self.recording_orb.configure(bg=RED if pulse > 0.5 else RED_DARK)

    def _build_content(self) -> None:
        self.content = tk.Frame(self, bg=BG)
        self.content.pack(fill="both", expand=True)
        self.tab_bar = tk.Frame(self.content, bg=BG)
        self.tab_bar.pack(fill="x", padx=16, pady=(12, 0))
        self.tabs = ttk.Notebook(self.content, style="Nexus.TNotebook")
        self.tabs.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        self.overview = tk.Frame(self.tabs, bg=BG)
        self.performance = tk.Frame(self.tabs, bg=BG)
        self.network_tab = tk.Frame(self.tabs, bg=BG)
        self.storage_tab = tk.Frame(self.tabs, bg=BG)
        self.hardware_tab = tk.Frame(self.tabs, bg=BG)
        self.session_tab = tk.Frame(self.tabs, bg=BG)
        self.tests = tk.Frame(self.tabs, bg=BG)
        tab_entries = (
            (self.overview, "OVERVIEW"), (self.performance, "PERFORMANCE"),
            (self.network_tab, "NETWORK"),
            (self.storage_tab, "STORAGE"), (self.hardware_tab, "HARDWARE"),
            (self.session_tab, "SESSION INSIGHTS"),
            (self.tests, "SELF-TEST")
        )
        self.tab_buttons: dict[tk.Frame, RoundedButton] = {}
        for column, (frame, label) in enumerate(tab_entries):
            self.tabs.add(frame, text=label)
            button = RoundedButton(
                self.tab_bar, text=label,
                command=lambda target=frame: self.tabs.select(target),
                bg=PANEL, fg=MUTED, activebackground=CARD_2,
                activeforeground=TEXT, padx=9, pady=6,
                font=(FONT_UI, 8, "bold"), radius=11
            )
            button.grid(
                row=0, column=column, sticky="nsew",
                padx=(0 if column == 0 else 3, 0)
            )
            self.tab_bar.columnconfigure(column, weight=1, uniform="nexus-tabs")
            self.tab_buttons[frame] = button
        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._build_overview()
        self._build_performance()
        self._build_network()
        self._build_storage()
        self._build_hardware()
        self._build_session()
        self._build_tests()
        self._build_compact()
        self._on_tab_changed()

    def _on_tab_changed(self, _event=None) -> None:
        selected = str(self.tabs.select())
        for frame, button in self.tab_buttons.items():
            button.set_selected(str(frame) == selected)
        if hasattr(self, "scanline"):
            self.scanline.burst()

    def _card(self, parent) -> RoundedPanel:
        return RoundedPanel(
            parent, surface=CARD, outline=BORDER, radius=20,
            border_width=2, padding=6
        )

    def _build_overview(self) -> None:
        hero = self._card(self.overview)
        hero.pack(fill="x", pady=(0, 10))
        rail = tk.Frame(hero, bg=RED, width=6)
        rail.pack(side="left", fill="y", padx=(7, 0), pady=9)
        hero_text = tk.Frame(hero, bg=CARD)
        hero_text.pack(side="left", fill="both", expand=True, padx=16, pady=10)
        tk.Label(hero_text, text="SYSTEM TELEMETRY", bg=CARD, fg=MUTED,
                 font=(FONT_UI, 8, "bold")).pack(anchor="w")
        self.hero_status = tk.Label(hero_text, text="INITIALISING", bg=CARD, fg=ORANGE,
                                    font=(FONT_DISPLAY, 18, "bold"))
        self.hero_status.pack(anchor="w")
        self.hero_detail = tk.Label(hero_text, text="Waiting for the first sensor sample", bg=CARD,
                                    fg=MUTED, font=(FONT_UI, 9))
        self.hero_detail.pack(anchor="w")
        chip_frame = tk.Frame(hero, bg=CARD)
        chip_frame.pack(side="right", fill="y", padx=15, pady=10)
        self.hero_uptime = self._hero_chip(chip_frame, "UPTIME")
        self.hero_peak = self._hero_chip(chip_frame, "SESSION PEAK CPU")
        self.hero_samples = self._hero_chip(chip_frame, "SAMPLES")

        gauges = self._card(self.overview)
        gauges.pack(fill="x", pady=(0, 10))
        self.cpu_gauge = Gauge(gauges, "CPU LOAD", CYAN)
        self.cpu_gauge.pack(side="left", expand=True, pady=10)
        self.memory_gauge = Gauge(gauges, "MEMORY USED", PURPLE)
        self.memory_gauge.pack(side="left", expand=True, pady=10)
        self.disk_gauge = Gauge(gauges, "SYSTEM VOLUME", GREEN)
        self.disk_gauge.pack(side="left", expand=True, pady=10)

        facts = tk.Frame(self.overview, bg=BG)
        facts.pack(fill="x")
        self.facts_frame = facts
        self.fact_labels: dict[str, tk.Label] = {}
        self.fact_cards: list[tk.Frame] = []
        self._overview_fact_columns = 0
        entries = (
            ("CPU", CYAN), ("GPU", GREEN), ("MEMORY", PURPLE),
            ("STORAGE", ORANGE), ("MOTHERBOARD", CYAN), ("OPERATING SYSTEM", GREEN),
        )
        for key, color in entries:
            card = self._card(facts)
            tk.Label(card, text=key, bg=CARD, fg=color,
                     font=(FONT_UI, 8, "bold")).pack(anchor="w", padx=14, pady=(9, 2))
            label = tk.Label(card, text="Waiting for data", bg=CARD, fg=TEXT,
                             justify="left", anchor="w", font=(FONT_DISPLAY, 10, "bold"))
            label.pack(anchor="w", fill="x", padx=14, pady=(0, 9))
            self.fact_cards.append(card)
            self.fact_labels[key] = label
        facts.bind("<Configure>", self._layout_overview_facts)

        self.overview_activity = tk.Frame(self.overview, bg=BG)
        self.overview_cpu_graph = HistoryGraph(
            self.overview_activity, "CPU - EXPANDED FULLSCREEN VIEW", CYAN
        )
        self.overview_memory_graph = HistoryGraph(
            self.overview_activity, "MEMORY - EXPANDED FULLSCREEN VIEW", PURPLE
        )
        self.overview_cpu_graph.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.overview_memory_graph.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self._overview_activity_visible = False
        self.overview.bind("<Configure>", self._resize_overview, add="+")

    def _layout_overview_facts(self, event=None) -> None:
        width = event.width if event is not None else self.facts_frame.winfo_width()
        columns = 3 if width >= 1350 else 2
        card_width = max(260, (width - 10 * (columns - 1)) // columns)
        if columns != self._overview_fact_columns:
            self._overview_fact_columns = columns
            for card in self.fact_cards:
                card.grid_forget()
            for column in range(3):
                self.facts_frame.columnconfigure(
                    column, weight=1 if column < columns else 0,
                    uniform="overview-facts" if column < columns else ""
                )
            for index, card in enumerate(self.fact_cards):
                row, column = divmod(index, columns)
                card.grid(row=row, column=column, sticky="nsew",
                          padx=(0, 5) if column == 0 else (5, 0), pady=5)
        for label in self.fact_labels.values():
            label.configure(wraplength=card_width - 28)

    def _resize_overview(self, event) -> None:
        should_show_activity = event.height >= 720
        if should_show_activity == self._overview_activity_visible:
            return
        self._overview_activity_visible = should_show_activity
        if should_show_activity:
            self.overview_activity.pack(fill="both", expand=True, pady=(10, 0))
        else:
            self.overview_activity.pack_forget()

    def _hero_chip(self, parent, title: str) -> tk.Label:
        frame = RoundedPanel(
            parent, surface=CARD_2, outline=BORDER, radius=13,
            border_width=2, padding=4
        )
        frame.pack(side="left", fill="y", padx=4)
        tk.Label(frame, text=title, bg=CARD_2, fg=MUTED,
                 font=(FONT_UI, 8, "bold")).pack(padx=12, pady=(6, 0))
        value = tk.Label(frame, text="--", bg=CARD_2, fg=TEXT,
                         font=(FONT_MONO, 10, "bold"))
        value.pack(padx=12, pady=(0, 6))
        return value

    def _build_performance(self) -> None:
        controls = tk.Frame(self.performance, bg=BG)
        controls.pack(fill="x", pady=(0, 6))
        tk.Label(controls, text="One-second samples; Task Manager may use different smoothing.",
                 bg=BG, fg=MUTED, font=(FONT_UI, 9)).pack(side="left")
        self.graph_pause_button = RoundedButton(
            controls, text="PAUSE GRAPHS", command=self.toggle_graphs, bg=CARD,
            fg=TEXT, activebackground=BORDER, activeforeground=TEXT, relief="flat",
            cursor="hand2", padx=10, pady=4, font=(FONT_UI, 8, "bold")
        )
        self.graph_pause_button.pack(side="right", padx=(6, 0))
        RoundedButton(
            controls, text="CLEAR", command=self.clear_graphs, bg=CARD,
            fg=MUTED, activebackground=BORDER, activeforeground=TEXT, relief="flat",
            cursor="hand2", padx=10, pady=4, font=(FONT_UI, 8, "bold")
        ).pack(side="right")
        self.cpu_graph = HistoryGraph(self.performance, "CPU LOAD - LIVE HISTORY", CYAN)
        self.cpu_graph.pack(fill="both", expand=True, pady=(0, 6))
        self.memory_graph = HistoryGraph(self.performance, "MEMORY USE - LIVE HISTORY", PURPLE)
        self.memory_graph.pack(fill="both", expand=True, pady=(6, 0))

    def toggle_graphs(self) -> None:
        self.graphs_paused = not self.graphs_paused
        self.graph_pause_button.configure(text="RESUME GRAPHS" if self.graphs_paused else "PAUSE GRAPHS")

    def clear_graphs(self) -> None:
        self.cpu_graph.clear()
        self.memory_graph.clear()
        self.overview_cpu_graph.clear()
        self.overview_memory_graph.clear()

    def _build_network(self) -> None:
        heading = self._card(self.network_tab)
        heading.pack(fill="x", pady=(0, 8))
        tk.Frame(heading, bg=CYAN, width=4).pack(side="left", fill="y")
        heading_text = tk.Frame(heading, bg=CARD)
        heading_text.pack(side="left", fill="both", expand=True, padx=16, pady=9)
        tk.Label(heading_text, text="LIVE NETWORK PULSE", bg=CARD, fg=CYAN,
                 font=(FONT_DISPLAY, 15, "bold")).pack(anchor="w")
        tk.Label(
            heading_text,
            text="Physical-adapter traffic includes LAN and VPN overhead. Link speed is not an internet speed test.",
            bg=CARD, fg=MUTED, font=(FONT_UI, 9)
        ).pack(anchor="w", pady=(2, 0))
        self.network_status = tk.Label(heading, text="SCANNING ADAPTERS", bg=CARD,
                                       fg=ORANGE, font=(FONT_UI, 8, "bold"))
        self.network_status.pack(side="right", padx=12)
        RoundedButton(
            heading, text="RESET TRAFFIC", command=self.reset_network,
            bg=CARD_2, fg=TEXT, activebackground=BORDER, activeforeground=TEXT,
            relief="flat", cursor="hand2", padx=10, pady=5,
            font=(FONT_UI, 8, "bold")
        ).pack(side="right", padx=(0, 8))

        summary = tk.Frame(self.network_tab, bg=BG)
        summary.pack(fill="x", pady=(0, 8))
        self.network_tiles: dict[str, MetricTile] = {}
        for column, (key, label, color) in enumerate((
            ("download", "DOWNLOAD NOW", CYAN),
            ("upload", "UPLOAD NOW", PURPLE),
            ("received", "SESSION RECEIVED", GREEN),
            ("sent", "SESSION SENT", ORANGE),
        )):
            tile = MetricTile(summary, label, color)
            tile.grid(row=0, column=column, sticky="nsew",
                      padx=(0 if column == 0 else 5, 0))
            summary.columnconfigure(column, weight=1, uniform="network-summary")
            self.network_tiles[key] = tile

        charts = tk.Frame(self.network_tab, bg=BG)
        charts.pack(fill="both", expand=True, pady=(0, 8))
        self.download_graph = RateHistoryGraph(charts, "DOWNLOAD - LIVE 60 SECONDS", CYAN)
        self.upload_graph = RateHistoryGraph(charts, "UPLOAD - LIVE 60 SECONDS", PURPLE)
        self.download_graph.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.upload_graph.pack(side="left", fill="both", expand=True, padx=(4, 0))

        deck = self._card(self.network_tab)
        deck.pack(fill="both", expand=True)
        deck_title = tk.Frame(deck, bg=CARD)
        deck_title.pack(fill="x", padx=13, pady=(8, 3))
        tk.Label(deck_title, text="CONNECTED PHYSICAL ADAPTERS", bg=CARD, fg=TEXT,
                 font=(FONT_UI, 8, "bold")).pack(side="left")
        tk.Label(deck_title, text="64-bit counters reported by the operating system", bg=CARD, fg=MUTED,
                 font=(FONT_UI, 8)).pack(side="right")
        self.network_canvas = tk.Canvas(deck, bg=CARD, highlightthickness=0, height=108)
        network_scrollbar = ttk.Scrollbar(
            deck, orient="vertical", command=self.network_canvas.yview
        )
        self.network_grid = tk.Frame(self.network_canvas, bg=CARD)
        self.network_grid.bind(
            "<Configure>",
            lambda _event: self.network_canvas.configure(
                scrollregion=self.network_canvas.bbox("all")
            )
        )
        self._network_window = self.network_canvas.create_window(
            (0, 0), window=self.network_grid, anchor="nw"
        )
        self.network_canvas.bind(
            "<Configure>",
            lambda event: (
                self.network_canvas.itemconfigure(self._network_window, width=event.width),
                self._layout_network_cards(event),
            )
        )
        self.network_canvas.configure(yscrollcommand=network_scrollbar.set)
        self.network_canvas.pack(side="left", fill="both", expand=True,
                                 padx=(8, 0), pady=(0, 8))
        network_scrollbar.pack(side="right", fill="y", padx=(0, 7), pady=(0, 8))
        self.network_cards: dict[int, NetworkAdapterCard] = {}
        self._network_columns = 0

    def reset_network(self) -> None:
        self.network_tracker.reset_session(time.monotonic())
        self.download_graph.clear()
        self.upload_graph.clear()
        if self.latest_network is not None:
            self.network_tiles["download"].set("0 B/s", "fresh baseline on next sample")
            self.network_tiles["upload"].set("0 B/s", "fresh baseline on next sample")
            self.network_tiles["received"].set("0 B", "since traffic reset")
            self.network_tiles["sent"].set("0 B", "since traffic reset")

    def _update_network(self, rates: NetworkRates) -> None:
        self.latest_network = rates
        self.network_tiles["download"].set(
            format_rate(rates.download_bps),
            f"peak {format_rate(rates.peak_download_bps)}"
        )
        self.network_tiles["upload"].set(
            format_rate(rates.upload_bps),
            f"peak {format_rate(rates.peak_upload_bps)}"
        )
        self.network_tiles["received"].set(
            format_bytes(rates.session_received_bytes), "since app/traffic reset"
        )
        self.network_tiles["sent"].set(
            format_bytes(rates.session_sent_bytes), "since app/traffic reset"
        )
        if not self.graphs_paused:
            self.download_graph.add(rates.download_bps)
            self.upload_graph.add(rates.upload_bps)

        current_luids = {adapter.luid for adapter in rates.adapters}
        layout_changed = False
        for stale_luid in set(self.network_cards) - current_luids:
            self.network_cards.pop(stale_luid).destroy()
            layout_changed = True
        for adapter in rates.adapters:
            card = self.network_cards.get(adapter.luid)
            if card is None:
                card = NetworkAdapterCard(self.network_grid, adapter.luid)
                self.network_cards[adapter.luid] = card
                layout_changed = True
            card.set(adapter)
        if layout_changed:
            self._layout_network_cards(force=True)
        if rates.adapters:
            suffix = "ADAPTER" if len(rates.adapters) == 1 else "ADAPTERS"
            self.network_status.configure(
                text=f"{len(rates.adapters)} PHYSICAL {suffix}", fg=GREEN
            )
        else:
            self.network_status.configure(text="NO CONNECTED ADAPTER", fg=ORANGE)

    def _layout_network_cards(self, event=None, force: bool = False) -> None:
        width = event.width if event is not None else self.network_canvas.winfo_width()
        columns = 2 if width >= 820 and len(self.network_cards) > 1 else 1
        if columns == self._network_columns and not force:
            return
        self._network_columns = columns
        for card in self.network_cards.values():
            card.grid_forget()
        for column in range(2):
            self.network_grid.columnconfigure(
                column, weight=1 if column < columns else 0,
                uniform="network-adapters" if column < columns else ""
            )
        ordered = sorted(
            self.network_cards.values(), key=lambda card: card.alias.cget("text").lower()
        )
        for index, card in enumerate(ordered):
            row, column = divmod(index, columns)
            card.grid(row=row, column=column, sticky="nsew", padx=4, pady=4)

    def _build_storage(self) -> None:
        heading = self._card(self.storage_tab)
        heading.pack(fill="x", pady=(0, 10))
        rail = tk.Frame(heading, bg=ORANGE, width=4)
        rail.pack(side="left", fill="y")
        heading_text = tk.Frame(heading, bg=CARD)
        heading_text.pack(side="left", fill="both", expand=True, padx=16, pady=12)
        tk.Label(heading_text, text="STORAGE VOLUME DECK", bg=CARD, fg=ORANGE,
                 font=(FONT_DISPLAY, 15, "bold")).pack(anchor="w")
        tk.Label(
            heading_text,
            text="Supported local volumes are detected automatically; capacity used is not disk activity.",
            bg=CARD, fg=MUTED, font=(FONT_UI, 9)
        ).pack(anchor="w", pady=(3, 0))
        self.storage_status = tk.Label(heading, text="SCANNING DRIVES", bg=CARD, fg=ORANGE,
                                       font=(FONT_UI, 8, "bold"))
        self.storage_status.pack(side="right", padx=18)

        summary = tk.Frame(self.storage_tab, bg=BG)
        summary.pack(fill="x", pady=(0, 10))
        self.storage_tiles: dict[str, MetricTile] = {}
        for column, (key, label, color) in enumerate((
            ("count", "STORAGE VOLUMES", CYAN),
            ("capacity", "TOTAL CAPACITY", PURPLE),
            ("free", "TOTAL FREE", GREEN),
            ("fullest", "MOST USED", ORANGE),
        )):
            tile = MetricTile(summary, label, color)
            tile.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0))
            summary.columnconfigure(column, weight=1, uniform="storage-summary")
            self.storage_tiles[key] = tile

        deck = self._card(self.storage_tab)
        deck.pack(fill="both", expand=True)
        deck_title = tk.Frame(deck, bg=CARD)
        deck_title.pack(fill="x", padx=16, pady=(13, 5))
        tk.Label(deck_title, text="VOLUMES", bg=CARD, fg=TEXT,
                 font=(FONT_UI, 9, "bold")).pack(side="left")
        tk.Label(deck_title, text="Normal < 75%  |  Elevated 75-89%  |  Critical 90%+", bg=CARD,
                 fg=MUTED, font=(FONT_UI, 8)).pack(side="right")
        self.drive_canvas = tk.Canvas(deck, bg=CARD, highlightthickness=0)
        drive_scrollbar = ttk.Scrollbar(deck, orient="vertical", command=self.drive_canvas.yview)
        self.drive_grid = tk.Frame(self.drive_canvas, bg=CARD)
        self.drive_grid.bind(
            "<Configure>",
            lambda _event: self.drive_canvas.configure(
                scrollregion=self.drive_canvas.bbox("all")
            )
        )
        self._drive_window = self.drive_canvas.create_window(
            (0, 0), window=self.drive_grid, anchor="nw"
        )
        self.drive_canvas.bind(
            "<Configure>",
            lambda event: (
                self.drive_canvas.itemconfigure(self._drive_window, width=event.width),
                self._layout_drive_cards(event),
            )
        )
        self.drive_canvas.configure(yscrollcommand=drive_scrollbar.set)
        self.drive_canvas.pack(side="left", fill="both", expand=True,
                               padx=(12, 0), pady=(4, 12))
        drive_scrollbar.pack(side="right", fill="y", padx=(0, 8), pady=(4, 12))
        self.drive_cards: dict[str, DriveCard] = {}
        self._drive_layout_signature: tuple[int, tuple[str, ...]] | None = None

    def _update_storage(self, snapshot) -> None:
        current_names = {drive.name for drive in snapshot.drives}
        layout_changed = False
        for stale_name in set(self.drive_cards) - current_names:
            self.drive_cards.pop(stale_name).destroy()
            layout_changed = True
        for drive in snapshot.drives:
            card = self.drive_cards.get(drive.name)
            if card is None:
                card = DriveCard(self.drive_grid, drive.name)
                self.drive_cards[drive.name] = card
                layout_changed = True
            card.set(drive, is_system=drive.name.rstrip("\\/") == snapshot.system_drive.rstrip("\\/"))

        count = len(snapshot.drives)
        total = sum(drive.total_gib for drive in snapshot.drives)
        free = sum(drive.free_gib for drive in snapshot.drives)
        fullest = max(snapshot.drives, key=lambda drive: drive.used_percent, default=None)
        self.storage_tiles["count"].set(str(count), "detected by NEXUS")
        self.storage_tiles["capacity"].set(f"{total:.1f} GiB", "combined local volumes")
        self.storage_tiles["free"].set(f"{free:.1f} GiB", "combined free capacity")
        if fullest is None:
            self.storage_tiles["fullest"].set("N/A", "no storage volumes")
            self.storage_status.configure(text="NO STORAGE DATA", fg=RED)
        else:
            self.storage_tiles["fullest"].set(
                f"{fullest.name}  {fullest.used_percent:.1f}%", "capacity used"
            )
            state_color = RED if fullest.used_percent >= 90 else ORANGE if fullest.used_percent >= 75 else GREEN
            status = "CAPACITY ALERT" if fullest.used_percent >= 90 else "CAPACITY ELEVATED" if fullest.used_percent >= 75 else "ALL VOLUMES NORMAL"
            self.storage_status.configure(text=status, fg=state_color)
        if layout_changed:
            self._layout_drive_cards(force=True)

    def _layout_drive_cards(self, event=None, force: bool = False) -> None:
        width = event.width if event is not None else self.drive_canvas.winfo_width()
        maximum_columns = 1 if width < 700 else 2 if width < 1400 else 3
        columns = min(maximum_columns, max(1, len(self.drive_cards)))
        names = tuple(sorted(self.drive_cards))
        signature = (columns, names)
        if signature == self._drive_layout_signature and not force:
            return
        self._drive_layout_signature = signature
        for card in self.drive_cards.values():
            card.grid_forget()
        for column in range(3):
            self.drive_grid.columnconfigure(
                column, weight=1 if column < columns else 0,
                uniform="drive-deck" if column < columns else ""
            )
        for index, name in enumerate(names):
            row, column = divmod(index, columns)
            self.drive_cards[name].grid(
                row=row, column=column, sticky="new", padx=5, pady=5
            )

    def _build_hardware(self) -> None:
        card = self._card(self.hardware_tab)
        card.pack(fill="both", expand=True)
        title = tk.Frame(card, bg=CARD)
        title.pack(fill="x", padx=20, pady=(17, 8))
        tk.Label(title, text="DETECTED HARDWARE", bg=CARD, fg=CYAN,
                 font=(FONT_DISPLAY, 10, "bold")).pack(side="left")
        tk.Label(title, text="Values reported by the operating system", bg=CARD, fg=MUTED,
                 font=(FONT_UI, 9)).pack(side="right")

        canvas = tk.Canvas(card, bg=CARD, highlightthickness=0)
        scrollbar = ttk.Scrollbar(card, orient="vertical", command=canvas.yview)
        self.inventory = tk.Frame(canvas, bg=CARD)
        self.inventory.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        window_id = canvas.create_window((0, 0), window=self.inventory, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=(0, 14))
        scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=(0, 14))
        self.inventory_labels: dict[str, tk.Label] = {}
        for name in (
            "Computer", "CPU", "CPU topology", "Reported CPU clock", "Graphics",
            "Reported memory", "Usable memory", "Motherboard", "BIOS", "Architecture",
            "Operating system", "Uptime", "Battery / power", "Temperatures / fans",
        ):
            self._inventory_row(name)
        self._inventory_row("Storage volumes")

    def _inventory_row(self, name: str) -> None:
        row = tk.Frame(self.inventory, bg=CARD)
        row.pack(fill="x", padx=8, pady=5)
        tk.Label(row, text=name, width=22, anchor="w", bg=CARD, fg=MUTED,
                 font=(FONT_UI, 9)).pack(side="left")
        label = tk.Label(row, text="Unavailable", anchor="w", justify="left",
                         wraplength=690, bg=CARD, fg=TEXT, font=(FONT_DISPLAY, 9, "bold"))
        label.pack(side="left", fill="x", expand=True)
        self.inventory_labels[name] = label

    def _build_session(self) -> None:
        header = self._card(self.session_tab)
        header.pack(fill="x", pady=(0, 10))
        left = tk.Frame(header, bg=CARD)
        left.pack(side="left", fill="both", expand=True, padx=18, pady=13)
        line = tk.Frame(left, bg=CARD)
        line.pack(anchor="w")
        self.recording_orb = tk.Label(line, text="REC", bg=RED, fg="white",
                                      font=(FONT_UI, 8, "bold"), padx=7, pady=2)
        self.recording_orb.pack(side="left", padx=(0, 9))
        tk.Label(line, text="SESSION INSIGHTS", bg=CARD, fg=TEXT,
                 font=(FONT_DISPLAY, 15, "bold")).pack(side="left")
        self.session_subtitle = tk.Label(
            left, text="Recording live samples; latest 86,400 remain exportable", bg=CARD, fg=MUTED,
            font=(FONT_UI, 9)
        )
        self.session_subtitle.pack(anchor="w", pady=(4, 0))
        actions = tk.Frame(header, bg=CARD)
        actions.pack(side="right", padx=16)
        self.record_button = RoundedButton(
            actions, text="PAUSE", command=self.toggle_recording, bg=RED, fg=TEXT,
            activebackground=ORANGE, activeforeground=TEXT, relief="flat",
            cursor="hand2", padx=14, pady=7, font=(FONT_UI, 8, "bold")
        )
        self.record_button.pack(side="left", padx=4)
        RoundedButton(
            actions, text="RESET", command=self.reset_session, bg=CARD_2, fg=TEXT,
            activebackground=BORDER, activeforeground=TEXT, relief="flat", cursor="hand2",
            padx=14, pady=7, font=(FONT_UI, 8, "bold")
        ).pack(side="left", padx=4)
        RoundedButton(
            actions, text="EXPORT CSV", command=self.export_session, bg=RED,
            fg=TEXT, activebackground=ORANGE, activeforeground=TEXT,
            relief="flat", cursor="hand2", padx=14, pady=7,
            font=(FONT_UI, 8, "bold")
        ).pack(side="left", padx=4)

        stats = tk.Frame(self.session_tab, bg=BG)
        stats.pack(fill="x", pady=(0, 10))
        self.session_tiles: dict[str, MetricTile] = {}
        for column, (key, label, color) in enumerate((
            ("duration", "ACTIVE RECORDING", CYAN), ("cpu_average", "AVG CPU", GREEN),
            ("cpu_peak", "PEAK CPU", ORANGE), ("memory_peak", "PEAK MEMORY", PURPLE),
            ("samples", "SAMPLES", CYAN), ("alerts", "ALERT SAMPLES", RED),
        )):
            tile = MetricTile(stats, label, color)
            tile.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 4, 0))
            stats.columnconfigure(column, weight=1, uniform="session-stats")
            self.session_tiles[key] = tile

        charts = tk.Frame(self.session_tab, bg=BG)
        charts.pack(fill="both", expand=True)
        self.session_cpu_graph = HistoryGraph(charts, "RECORDED CPU - LATEST 60 SAMPLES", CYAN)
        self.session_memory_graph = HistoryGraph(charts, "RECORDED MEMORY - LATEST 60 SAMPLES", PURPLE)
        self.session_cpu_graph.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.session_memory_graph.pack(side="left", fill="both", expand=True, padx=(5, 0))

        event_card = self._card(self.session_tab)
        event_card.pack(fill="x", pady=(10, 0))
        tk.Label(event_card, text="THRESHOLD EVENTS", bg=CARD, fg=MUTED,
                 font=(FONT_UI, 8, "bold")).pack(anchor="w", padx=13, pady=(8, 2))
        self.session_event_label = tk.Label(
            event_card, text="No threshold events. Alerts use CPU/RAM >= 85% and storage >= 90%.",
            bg=CARD, fg=GREEN, anchor="w", justify="left", font=(FONT_UI, 9)
        )
        self.session_event_label.pack(fill="x", padx=13, pady=(0, 8))
        self.session_events: deque[str] = deque(maxlen=3)
        self.last_session_alert = ""
        self._update_session_ui()

    def toggle_recording(self) -> None:
        if self.session.active:
            self.session.pause()
            self.record_button.configure(text="RESUME", bg=GREEN)
            self.recording_orb.configure(text="PAUSED", bg=ORANGE)
            self.session_subtitle.configure(text="Recording paused; live dashboard remains active")
        else:
            self.session.resume()
            self.record_button.configure(text="PAUSE", bg=ORANGE)
            self.recording_orb.configure(text="REC", bg=RED)
            self.session_subtitle.configure(
                text="Recording live samples; latest 86,400 remain exportable"
            )

    def reset_session(self) -> None:
        self.session.reset()
        self.session_cpu_graph.clear()
        self.session_memory_graph.clear()
        self.session_events.clear()
        self.last_session_alert = ""
        self.session_event_label.configure(
            text="Session reset. Waiting for new samples.", fg=GREEN
        )
        self.record_button.configure(text="PAUSE", bg=ORANGE)
        self.recording_orb.configure(text="REC", bg=RED)
        self.session_subtitle.configure(text="Recording a new session")
        self._update_session_ui()

    def export_session(self) -> None:
        if not self.session.sample_count:
            messagebox.showinfo("Nothing to export", "Record at least one sample first.", parent=self)
            return
        root = Path(__file__).resolve().parents[1]
        default_name = f"nexus_session_{datetime.now():%Y%m%d_%H%M%S}.csv"
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="Export NEXUS session",
            initialdir=root,
            initialfile=default_name,
            defaultextension=".csv",
            filetypes=(("CSV telemetry", "*.csv"), ("All files", "*.*")),
        )
        if not destination:
            return
        try:
            path = self.session.export_csv(destination)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        messagebox.showinfo("Session exported", f"Saved {self.session.sample_count} samples to:\n{path}", parent=self)

    def _update_session_ui(self, summary: dict[str, object] | None = None) -> None:
        if summary is None:
            summary = self.session.summary()
        self.session_tiles["duration"].set(
            duration_text(summary["duration_seconds"]), "paused time excluded"
        )
        self.session_tiles["cpu_average"].set(value_text(summary["cpu_average"], "%"), "mean recorded load")
        self.session_tiles["cpu_peak"].set(value_text(summary["cpu_peak"], "%"), "highest recorded load")
        self.session_tiles["memory_peak"].set(value_text(summary["memory_peak"], "%"), "highest recorded use")
        retained = int(summary.get("retained_samples", summary["samples"]))
        sample_detail = (
            f"{retained:,} latest exportable"
            if retained != summary["samples"] else "one per live refresh"
        )
        self.session_tiles["samples"].set(str(summary["samples"]), sample_detail)
        self.session_tiles["alerts"].set(str(summary["alert_samples"]), "threshold samples")

    def _build_tests(self) -> None:
        card = self._card(self.tests)
        card.pack(fill="both", expand=True)
        tk.Label(card, text="SAFE QUICK CHECK", bg=CARD, fg=TEXT,
                 font=(FONT_DISPLAY, 19, "bold")).pack(pady=(34, 7))
        tk.Label(
            card,
            text=("Validates a known SHA-256 calculation and temporary-file integrity.\n"
                  "Throughput is cache-affected and is not a full physical-drive benchmark."),
            bg=CARD, fg=MUTED, justify="center", font=(FONT_UI, 10)
        ).pack()
        self.test_button = RoundedButton(
            card, text="RUN QUICK CHECK", command=self.run_tests, bg=RED,
            fg=TEXT, activebackground=ORANGE, activeforeground=TEXT,
            relief="flat", cursor="hand2", padx=28, pady=11,
            font=(FONT_UI, 9, "bold")
        )
        self.test_button.pack(pady=22)
        self.test_output = tk.Text(card, height=10, bg=CARD_2, fg=TEXT,
                                   insertbackground=RED, relief="flat", padx=16, pady=13,
                                   font=(FONT_MONO, 10), highlightthickness=2,
                                   highlightbackground=BORDER, highlightcolor=RED)
        self.test_output.pack(fill="x", padx=40)
        self._set_test_text("Ready. Click RUN QUICK CHECK to begin.\n")

    def _build_compact(self) -> None:
        self.compact_panel = self._card(self.content)
        top = tk.Frame(self.compact_panel, bg=CARD)
        top.pack(fill="x", padx=16, pady=(12, 7))
        self.hud_drag_handle = tk.Label(top, text="NEXUS // DESKTOP HUD", bg=CARD, fg=CYAN,
                                        cursor="fleur", font=(FONT_DISPLAY, 9, "bold"))
        self.hud_drag_handle.pack(side="left")
        self.compact_updated = tk.Label(top, text="Waiting", bg=CARD, fg=MUTED,
                                        font=(FONT_UI, 8))
        self.compact_updated.pack(side="left", padx=14)
        RoundedButton(
            top, text="X", command=self.close, bg=CARD_2, fg=RED,
            activebackground=RED, activeforeground=TEXT, relief="flat",
            cursor="hand2", padx=8, pady=3, font=(FONT_UI, 8, "bold"), radius=9
        ).pack(side="right")
        RoundedButton(
            top, text="RESTORE", command=self.exit_hud, bg=CARD_2, fg=TEXT,
            activebackground=BORDER, activeforeground=TEXT, relief="flat",
            cursor="hand2", padx=10, pady=3, font=(FONT_UI, 8, "bold"), radius=9
        ).pack(side="right", padx=6)
        tiles = tk.Frame(self.compact_panel, bg=CARD)
        tiles.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        hud_cells = []
        for column in range(3):
            cell = RoundedPanel(
                tiles, surface=CARD_2, outline=BORDER, radius=17,
                border_width=2, padding=5
            )
            cell.grid(row=0, column=column, sticky="nsew", padx=5)
            hud_cells.append(cell)
            tiles.columnconfigure(column, weight=1, uniform="compact")
        tiles.rowconfigure(0, weight=1)
        self.compact_cpu = MetricTile(hud_cells[0], "CPU LOAD", CYAN)
        self.compact_cpu.pack(fill="x")
        self.hud_cpu_graph = MiniSparkline(hud_cells[0], CYAN)
        self.hud_cpu_graph.pack(fill="both", expand=True, padx=8, pady=(0, 7))
        self.compact_memory = MetricTile(hud_cells[1], "MEMORY", PURPLE)
        self.compact_memory.pack(fill="x")
        self.hud_memory_graph = MiniSparkline(hud_cells[1], PURPLE)
        self.hud_memory_graph.pack(fill="both", expand=True, padx=8, pady=(0, 7))
        self.compact_disk = MetricTile(hud_cells[2], "STORAGE USED", GREEN)
        self.compact_disk.pack(fill="x")
        opacity = tk.Frame(hud_cells[2], bg=CARD_2)
        opacity.pack(fill="x", padx=8, pady=(3, 7))
        tk.Label(opacity, text="OPACITY", bg=CARD_2, fg=MUTED,
                 font=(FONT_UI, 8, "bold")).pack(anchor="w")
        for percent in (70, 85, 100):
            RoundedButton(
                opacity, text=str(percent),
                command=lambda p=percent: self.set_hud_opacity(p),
                bg=CARD, fg=TEXT, activebackground=BORDER,
                activeforeground=TEXT, relief="flat", cursor="hand2",
                padx=6, pady=2, font=(FONT_UI, 8), radius=8
            ).pack(side="left", padx=(0, 4), pady=(5, 0))
        for widget in (top, self.hud_drag_handle, self.compact_updated):
            widget.bind("<ButtonPress-1>", self._start_hud_drag)
            widget.bind("<B1-Motion>", self._drag_hud)

    def _build_footer(self) -> None:
        footer = tk.Frame(self, bg=PANEL, height=34)
        self.footer = footer
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        self.updated_label = tk.Label(footer, text="Waiting for first reading", bg=PANEL,
                                      fg=MUTED, font=(FONT_UI, 8))
        self.updated_label.pack(side="left", padx=18)
        github = tk.Label(footer, text="Made by Kieranmcm07  |  GitHub", bg=PANEL,
                          fg=RED, cursor="hand2", font=(FONT_UI, 8, "bold"))
        github.pack(side="right", padx=18)
        github.bind("<Button-1>", lambda _e: webbrowser.open_new_tab(GITHUB_URL))

    @staticmethod
    def _replace_latest(target: queue.Queue, item: object) -> None:
        """Put without blocking, discarding an older queued value if necessary."""
        try:
            target.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            target.get_nowait()
        except queue.Empty:
            pass
        try:
            target.put_nowait(item)
        except queue.Full:
            # Another producer won the race; its value is at least as fresh.
            pass

    def _sampler(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self._replace_latest(self.snapshot_queue, ("snapshot", take_snapshot()))
            except Exception as exc:
                self._replace_latest(self.snapshot_queue, ("sensor_error", exc))
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.05, 1.0 - elapsed))

    def _poll_results(self) -> None:
        try:
            kind, result = self.snapshot_queue.get_nowait()
            if kind == "snapshot":
                self._render_snapshot(result)
            else:
                self.sensor_state = "error"
                self.live_status.configure(text="!  SENSOR ERROR", fg=RED)
                self.updated_label.configure(text=f"Sensor error: {str(result)[:100]}")
                if not self.graphs_paused:
                    self.cpu_graph.add(None)
                    self.memory_graph.add(None)
                    self.overview_cpu_graph.add(None)
                    self.overview_memory_graph.add(None)
                    self.download_graph.add(None)
                    self.upload_graph.add(None)
                self.hud_cpu_graph.add(None)
                self.hud_memory_graph.add(None)
        except queue.Empty:
            pass
        try:
            while True:
                kind, result = self.results.get_nowait()
                if kind == "tests":
                    self._render_tests(result)
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.after(100, self._poll_results)

    def _render_snapshot(self, snapshot) -> None:
        self.latest_snapshot = snapshot
        self.sensor_state = "live"
        timestamp = datetime.fromtimestamp(snapshot.captured_at).strftime("%H:%M:%S")
        self.live_status.configure(text="LIVE", fg=GREEN)
        self.compact_updated.configure(text=f"Updated {timestamp}")
        rates = self.network_tracker.update(
            getattr(snapshot, "network_interfaces", ()),
            getattr(snapshot, "monotonic_at", time.monotonic()),
        )
        self._update_network(rates)
        self.updated_label.configure(
            text=(f"Updated {timestamp}  |  NET down {format_rate(rates.download_bps)}"
                  f"  up {format_rate(rates.upload_bps)}  |  1 sec refresh")
        )
        self.cpu_gauge.set(snapshot.cpu_usage_percent)
        self.memory_gauge.set(snapshot.memory_used_percent)
        self.disk_gauge.title = f"{snapshot.system_drive} STORAGE USED"
        self.disk_gauge.set(snapshot.disk_used_percent)
        if not self.graphs_paused:
            self.cpu_graph.add(snapshot.cpu_usage_percent)
            self.memory_graph.add(snapshot.memory_used_percent)
            self.overview_cpu_graph.add(snapshot.cpu_usage_percent)
            self.overview_memory_graph.add(snapshot.memory_used_percent)
        self.hud_cpu_graph.add(snapshot.cpu_usage_percent)
        self.hud_memory_graph.add(snapshot.memory_used_percent)
        self._update_storage(snapshot)

        captured = self.session.capture(snapshot, rates)
        if captured is not None:
            self.session_cpu_graph.add(snapshot.cpu_usage_percent)
            self.session_memory_graph.add(snapshot.memory_used_percent)
            if captured.alert and captured.alert != self.last_session_alert:
                event = f"{timestamp}  {captured.alert}"
                self.session_events.appendleft(event)
                self.session_event_label.configure(text="   |   ".join(self.session_events), fg=RED)
            elif not captured.alert and self.last_session_alert:
                self.session_events.appendleft(f"{timestamp}  Thresholds returned to normal")
                self.session_event_label.configure(text="   |   ".join(self.session_events), fg=GREEN)
            self.last_session_alert = captured.alert
        session_summary = self.session.summary()
        self._update_session_ui(session_summary)

        high_cpu = snapshot.cpu_usage_percent is not None and snapshot.cpu_usage_percent >= 85
        high_memory = snapshot.memory_used_percent is not None and snapshot.memory_used_percent >= 85
        critical_drives = [drive for drive in snapshot.drives if drive.used_percent >= 90]
        elevated_drives = [drive for drive in snapshot.drives if drive.used_percent >= 75]
        storage_alert = bool(critical_drives)
        if high_cpu or high_memory or storage_alert:
            self.hero_status.configure(text="ATTENTION", fg=RED)
            reasons = []
            if high_cpu: reasons.append("CPU load >= 85%")
            if high_memory: reasons.append("memory use >= 85%")
            if storage_alert:
                reasons.append(
                    "drive capacity >= 90%: " + ", ".join(drive.name for drive in critical_drives)
                )
            self.hero_detail.configure(text="  |  ".join(reasons))
        elif ((snapshot.cpu_usage_percent or 0) >= 65 or
              (snapshot.memory_used_percent or 0) >= 65 or elevated_drives):
            if elevated_drives:
                self.hero_status.configure(text="CAPACITY ELEVATED", fg=ORANGE)
                self.hero_detail.configure(
                    text="75%+ used: " + ", ".join(
                        f"{drive.name} {drive.used_percent:.1f}%" for drive in elevated_drives
                    )
                )
            else:
                self.hero_status.configure(text="ELEVATED LOAD", fg=ORANGE)
                self.hero_detail.configure(text="A live resource is above 65%; this is informational")
        else:
            self.hero_status.configure(text="SYSTEM NOMINAL", fg=GREEN)
            self.hero_detail.configure(text="All displayed thresholds are within the normal band")
        self.hero_uptime.configure(text=uptime_text(snapshot.uptime_seconds))
        self.hero_peak.configure(text=value_text(session_summary["cpu_peak"], "%"))
        self.hero_samples.configure(text=str(session_summary["samples"]))

        cores = (f"{snapshot.physical_cores} cores / {snapshot.logical_cpus} threads"
                 if snapshot.physical_cores else f"{snapshot.logical_cpus} logical processors")
        gpu_text = ", ".join(self.hardware.gpu_names) or "No display adapter reported"
        self.fact_labels["CPU"].configure(text=f"{snapshot.processor}  |  {cores}")
        self.fact_labels["GPU"].configure(text=gpu_text)
        self.fact_labels["MEMORY"].configure(
            text=f"{value_text(snapshot.memory_installed_gib, ' GiB')} reported  |  "
                 f"{value_text(snapshot.memory_used_gib, ' GiB')} in use"
        )
        overview_drives = "\n".join(
            f"{drive.name}  {drive.used_percent:.1f}% used  |  "
            f"{drive.free_gib:.1f} GiB free / {drive.total_gib:.1f} GiB"
            for drive in snapshot.drives
        ) or "No storage volume data available"
        self.fact_labels["STORAGE"].configure(text=overview_drives)
        self.fact_labels["MOTHERBOARD"].configure(text=self.hardware.motherboard)
        self.fact_labels["OPERATING SYSTEM"].configure(text=snapshot.operating_system)

        power = "No battery detected"
        if snapshot.battery_percent is not None:
            state = "AC connected" if snapshot.plugged_in is True else "On battery" if snapshot.plugged_in is False else "Power state unknown"
            power = f"{snapshot.battery_percent}% - {state}"
        drives = "\n".join(
            f"{drive.name}  {drive.free_gib:.1f} GiB free / {drive.total_gib:.1f} GiB  ({drive.used_percent:.1f}% used)"
            for drive in snapshot.drives
        ) or "No storage volume data available"
        inventory = {
            "Computer": snapshot.computer,
            "CPU": snapshot.processor,
            "CPU topology": cores,
            "Reported CPU clock": (f"{self.hardware.cpu_max_mhz} MHz (system-reported)"
                                   if self.hardware.cpu_max_mhz else "Unavailable"),
            "Graphics": gpu_text,
            "Reported memory": value_text(snapshot.memory_installed_gib, " GiB"),
            "Usable memory": (f"{value_text(snapshot.memory_total_gib, ' GiB')} total, "
                              f"{value_text(snapshot.memory_available_gib, ' GiB')} available"),
            "Motherboard": self.hardware.motherboard,
            "BIOS": self.hardware.bios_version,
            "Architecture": self.hardware.architecture,
            "Operating system": snapshot.operating_system,
            "Uptime": uptime_text(snapshot.uptime_seconds),
            "Battery / power": power,
            "Temperatures / fans": "Unavailable - requires a trusted hardware sensor provider",
            "Storage volumes": drives,
        }
        for name, value in inventory.items():
            self.inventory_labels[name].configure(text=value)

        self.compact_cpu.set(value_text(snapshot.cpu_usage_percent, "%"), cores)
        self.compact_memory.set(value_text(snapshot.memory_used_percent, "%"),
                                f"{value_text(snapshot.memory_used_gib, ' GiB')} used")
        fullest_drive = max(snapshot.drives, key=lambda drive: drive.used_percent, default=None)
        if fullest_drive is None:
            self.compact_disk.set("N/A", "No storage volume data")
        else:
            self.compact_disk.set(
                f"{fullest_drive.name}  {fullest_drive.used_percent:.1f}%",
                f"most used; {fullest_drive.free_gib:.1f} GiB free"
            )

    def toggle_compact(self) -> None:
        if self.compact:
            self.exit_hud()
            return
        self.normal_state = "zoomed" if self._is_window_maximized() else self.state()
        center_x = self.winfo_rootx() + self.winfo_width() // 2
        center_y = self.winfo_rooty() + self.winfo_height() // 2
        work_left, work_top, work_right, work_bottom = self._monitor_work_area(
            center_x, center_y
        )
        if self.normal_state == "zoomed":
            # Maximized windows can ignore a small geometry request. Normalize
            # first, then retain the real restore rectangle.
            self._set_window_maximized(False)
            self.update_idletasks()
        self.compact = True
        self.normal_geometry = self.geometry()
        self.tabs.pack_forget()
        self.tab_bar.pack_forget()
        self.header.pack_forget()
        self.scanline.pack_forget()
        self.footer.pack_forget()
        self.compact_panel.pack(fill="both", expand=True, padx=2, pady=2)
        self.minsize(620, 280)
        width, height = 720, 330
        x = max(work_left + 10, work_right - width - 30)
        y = max(work_top + 10, min(work_top + 30, work_bottom - height - 10))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self._set_window_attribute("-topmost", True)
        self._set_window_attribute("-alpha", 0.92)
        self._set_borderless(True)
        self.hud_borderless = True

    def exit_hud(self) -> None:
        if not self.compact:
            return
        self._set_borderless(False)
        self.hud_borderless = False
        self._set_window_attribute("-topmost", False)
        self._set_window_attribute("-alpha", 1.0)
        self.compact_panel.pack_forget()
        self.header.pack(fill="x", before=self.content)
        self.scanline.pack(fill="x", before=self.content)
        self.tab_bar.pack(fill="x", padx=16, pady=(12, 0))
        self.tabs.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        self.footer.pack(fill="x", side="bottom")
        self.minsize(860, 640)
        self.geometry(self.normal_geometry)
        self.compact = False
        if self.normal_state == "zoomed":
            self.after(
                50,
                lambda: self._set_window_maximized(True) if not self.compact else None,
            )
        self.after(50, self._enable_dark_titlebar)

    def set_hud_opacity(self, percent: int) -> None:
        if self.compact:
            self._set_window_attribute(
                "-alpha", max(0.55, min(1.0, percent / 100))
            )

    def _set_window_attribute(self, name: str, value: object) -> None:
        """Apply an optional window-manager hint without breaking the HUD."""
        try:
            self.attributes(name, value)
        except tk.TclError:
            # Some X11/Wayland window managers do not implement every Tk hint.
            pass

    def _is_window_maximized(self) -> bool:
        if platform.system() == "Windows":
            return self.state() == "zoomed"
        try:
            return bool(self.attributes("-zoomed"))
        except tk.TclError:
            return self.state() == "zoomed"

    def _set_window_maximized(self, enabled: bool) -> None:
        try:
            if platform.system() == "Windows":
                self.state("zoomed" if enabled else "normal")
            else:
                self.attributes("-zoomed", enabled)
        except tk.TclError:
            try:
                self.state("zoomed" if enabled else "normal")
            except tk.TclError:
                pass

    def _set_borderless(self, enabled: bool) -> None:
        try:
            self.overrideredirect(enabled)
        except tk.TclError:
            pass

    def _start_hud_drag(self, event) -> None:
        self._drag_origin = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_hud(self, event) -> None:
        if not self.compact:
            return
        x = event.x_root - self._drag_origin[0]
        y = event.y_root - self._drag_origin[1]
        left, top, right, bottom = self._monitor_work_area(event.x_root, event.y_root)
        x = max(left, min(x, right - self.winfo_width()))
        y = max(top, min(y, bottom - self.winfo_height()))
        self.geometry(f"{x:+d}{y:+d}")

    def _monitor_work_area(self, x: int, y: int) -> tuple[int, int, int, int]:
        """Return the nearest work area or the virtual desktop as a fallback."""
        if platform.system() == "Windows":
            try:
                import ctypes

                class Point(ctypes.Structure):
                    _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))

                class Rect(ctypes.Structure):
                    _fields_ = (
                        ("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
                    )

                class MonitorInfo(ctypes.Structure):
                    _fields_ = (
                        ("size", ctypes.c_ulong), ("monitor", Rect),
                        ("work", Rect), ("flags", ctypes.c_ulong),
                    )

                user32 = ctypes.windll.user32
                user32.MonitorFromPoint.argtypes = (Point, ctypes.c_ulong)
                user32.MonitorFromPoint.restype = ctypes.c_void_p
                user32.GetMonitorInfoW.argtypes = (
                    ctypes.c_void_p, ctypes.POINTER(MonitorInfo)
                )
                user32.GetMonitorInfoW.restype = ctypes.c_int
                monitor = user32.MonitorFromPoint(Point(x, y), 2)  # nearest monitor
                info = MonitorInfo()
                info.size = ctypes.sizeof(info)
                if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    return (
                        int(info.work.left), int(info.work.top),
                        int(info.work.right), int(info.work.bottom),
                    )
            except Exception:
                pass
        # Tk's virtual root covers the whole X11 desktop where the window
        # manager exposes it, including monitor layouts wider than one screen.
        try:
            left = int(self.winfo_vrootx())
            top = int(self.winfo_vrooty())
            width = int(self.winfo_vrootwidth())
            height = int(self.winfo_vrootheight())
            if width > 0 and height > 0:
                return (left, top, left + width, top + height)
        except tk.TclError:
            pass
        return (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())

    def run_tests(self) -> None:
        self.test_button.configure(state="disabled", text="CHECKING...")
        self._set_test_text("Checking a known CPU calculation...\nChecking temporary-file integrity...\n")
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self) -> None:
        try:
            self._replace_latest(
                self.results, ("tests", (cpu_self_test(), disk_self_test()))
            )
        except Exception as exc:
            self._replace_latest(self.results, ("tests", exc))

    def _render_tests(self, result: object) -> None:
        self.test_button.configure(state="normal", text="RUN AGAIN")
        if isinstance(result, Exception):
            self._set_test_text(f"CHECK ERROR: {result}\n")
            return
        cpu, disk = result
        cpu_note = "known vector valid" if cpu.get("validated") else "calculation mismatch"
        text = (
            f"CPU CALCULATION  [{cpu['status']}]  {cpu_note}\n"
            f"CPU WORKLOAD     {cpu['sha256_blocks']:,} hash blocks processed\n"
            f"FILE INTEGRITY   [{disk['status']}]  {disk['size_mb']} MiB matched after read-back\n\n"
            f"Cache-affected temporary-file estimate:\n"
            f"  write {disk['write_mb_s']} MiB/s  |  read+hash {disk['read_mb_s']} MiB/s\n"
        )
        self._set_test_text(text)

    def _set_test_text(self, text: str) -> None:
        self.test_output.configure(state="normal")
        self.test_output.delete("1.0", "end")
        self.test_output.insert("end", text)
        self.test_output.configure(state="disabled")

    def close(self) -> None:
        self.stop_event.set()
        if self._animation_job is not None:
            try:
                self.after_cancel(self._animation_job)
            except tk.TclError:
                pass
            self._animation_job = None
        self.destroy()


def main() -> None:
    HardwareDashboard().mainloop()


if __name__ == "__main__":
    main()
