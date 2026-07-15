from __future__ import annotations

import platform
import queue
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque
from datetime import datetime
from tkinter import ttk

from hardware_monitor.monitor import (
    cpu_self_test,
    disk_self_test,
    hardware_info,
    take_snapshot,
)


BG = "#060a13"
PANEL = "#0a1222"
CARD = "#0e1a30"
CARD_2 = "#101f38"
BORDER = "#1c3457"
TEXT = "#eef7ff"
MUTED = "#7e95b5"
CYAN = "#20dcff"
GREEN = "#26efa1"
PURPLE = "#a775ff"
ORANGE = "#ffb454"
RED = "#ff5d78"

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


class Gauge(tk.Canvas):
    def __init__(self, parent, title: str, color: str, size: int = 145):
        super().__init__(parent, width=size, height=size, bg=CARD, highlightthickness=0)
        self.size = size
        self.title = title
        self.color = color
        self.value: float | None = None
        self.bind("<Configure>", lambda _event: self.draw())

    def set(self, value: float | None) -> None:
        self.value = None if value is None else max(0.0, min(100.0, float(value)))
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        s, pad = self.size, 14
        self.create_arc(pad, pad, s - pad, s - pad, start=225, extent=-270,
                        style="arc", width=10, outline="#1a2b47")
        if self.value is not None:
            self.create_arc(pad, pad, s - pad, s - pad, start=225,
                            extent=-270 * self.value / 100, style="arc", width=10,
                            outline=self.color)
            reading = f"{self.value:.0f}%"
        else:
            reading = "N/A"
        self.create_text(s / 2, s / 2 - 5, text=reading, fill=TEXT,
                         font=("Segoe UI Semibold", 24))
        self.create_text(s / 2, s / 2 + 25, text=self.title, fill=MUTED,
                         font=("Segoe UI", 9, "bold"))


class HistoryGraph(tk.Canvas):
    def __init__(self, parent, title: str, color: str):
        super().__init__(parent, bg=CARD, height=190, highlightthickness=0)
        self.title = title
        self.color = color
        self.values: deque[float] = deque(maxlen=60)
        self.bind("<Configure>", lambda _event: self.draw())

    def add(self, value: float | None) -> None:
        if value is not None:
            self.values.append(float(value))
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 120)
        height = max(self.winfo_height(), 110)
        self.create_text(18, 19, anchor="w", text=self.title, fill=TEXT,
                         font=("Segoe UI Semibold", 11))
        if self.values:
            current = self.values[-1]
            average = sum(self.values) / len(self.values)
            peak = max(self.values)
            stats = f"NOW {current:.1f}%    AVG {average:.1f}%    PEAK {peak:.1f}%"
        else:
            stats = "WAITING FOR SENSOR DATA"
        self.create_text(width - 18, 19, anchor="e", text=stats, fill=self.color,
                         font=("Segoe UI", 9, "bold"))
        top, bottom = 48, height - 22
        for percent in (0, 25, 50, 75, 100):
            y = bottom - (bottom - top) * percent / 100
            self.create_line(42, y, width - 16, y, fill="#172943")
            if percent in (0, 50, 100):
                self.create_text(34, y, anchor="e", text=str(percent), fill="#536b8d",
                                 font=("Segoe UI", 7))
        if not self.values:
            return
        values = list(self.values)
        points: list[float] = []
        for index, value in enumerate(values):
            x = 42 + (width - 58) * index / max(1, len(values) - 1)
            y = bottom - (bottom - top) * max(0, min(100, value)) / 100
            points.extend((x, y))
        if len(points) >= 4:
            self.create_polygon([42, bottom] + points + [points[-2], bottom],
                                fill="#102b42", outline="")
            self.create_line(points, fill=self.color, width=2, smooth=True)
        elif points:
            x, y = points
            self.create_oval(x - 2, y - 2, x + 2, y + 2, fill=self.color, outline="")


class MetricTile(tk.Frame):
    def __init__(self, parent, label: str, color: str):
        super().__init__(parent, bg=CARD_2, highlightbackground=BORDER, highlightthickness=1)
        tk.Label(self, text=label, bg=CARD_2, fg=color,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=13, pady=(10, 2))
        self.value = tk.Label(self, text="N/A", bg=CARD_2, fg=TEXT,
                              font=("Segoe UI Semibold", 16))
        self.value.pack(anchor="w", padx=13)
        self.detail = tk.Label(self, text="Waiting for data", bg=CARD_2, fg=MUTED,
                               font=("Segoe UI", 8))
        self.detail.pack(anchor="w", padx=13, pady=(0, 10))

    def set(self, value: str, detail: str) -> None:
        self.value.configure(text=value)
        self.detail.configure(text=detail)


class HardwareDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NEXUS - Hardware Monitor")
        self.geometry("1100x760")
        self.minsize(860, 640)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.results: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.compact = False
        self.normal_geometry = "1100x760"
        self.latest_snapshot = None
        self.hardware = hardware_info()
        self._configure_style()
        self._build_header()
        self._build_content()
        self._build_footer()
        self.after(50, self._enable_dark_titlebar)
        self.after(100, self._poll_results)
        threading.Thread(target=self._sampler, daemon=True).start()

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                        padding=(20, 11), borderwidth=0, font=("Segoe UI", 9, "bold"))
        style.map("TNotebook.Tab", background=[("selected", CARD)],
                  foreground=[("selected", CYAN)])

    def _enable_dark_titlebar(self) -> None:
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
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="NEXUS", bg=PANEL, fg=TEXT,
                 font=("Segoe UI Semibold", 19)).pack(side="left", padx=(22, 6))
        tk.Label(header, text="// HARDWARE MONITOR", bg=PANEL, fg=CYAN,
                 font=("Segoe UI", 9, "bold")).pack(side="left", pady=(6, 0))
        self.live_status = tk.Label(header, text="*  STARTING", bg=PANEL, fg=ORANGE,
                                    font=("Segoe UI", 9, "bold"))
        self.live_status.pack(side="right", padx=20)
        self.compact_button = tk.Button(
            header, text="COMPACT MODE", command=self.toggle_compact, bg=CARD,
            fg=TEXT, activebackground=BORDER, activeforeground=TEXT, relief="flat",
            cursor="hand2", padx=13, pady=7, font=("Segoe UI", 8, "bold")
        )
        self.compact_button.pack(side="right")

    def _build_content(self) -> None:
        self.content = tk.Frame(self, bg=BG)
        self.content.pack(fill="both", expand=True)
        self.tabs = ttk.Notebook(self.content)
        self.tabs.pack(fill="both", expand=True, padx=16, pady=14)
        self.overview = tk.Frame(self.tabs, bg=BG)
        self.performance = tk.Frame(self.tabs, bg=BG)
        self.hardware_tab = tk.Frame(self.tabs, bg=BG)
        self.tests = tk.Frame(self.tabs, bg=BG)
        for frame, label in (
            (self.overview, "OVERVIEW"), (self.performance, "PERFORMANCE"),
            (self.hardware_tab, "HARDWARE"), (self.tests, "SELF-TEST")
        ):
            self.tabs.add(frame, text=label)
        self._build_overview()
        self._build_performance()
        self._build_hardware()
        self._build_tests()
        self._build_compact()

    def _card(self, parent) -> tk.Frame:
        return tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)

    def _build_overview(self) -> None:
        gauges = self._card(self.overview)
        gauges.pack(fill="x", pady=(0, 10))
        self.cpu_gauge = Gauge(gauges, "CPU LOAD", CYAN)
        self.cpu_gauge.pack(side="left", expand=True, pady=10)
        self.memory_gauge = Gauge(gauges, "MEMORY USED", PURPLE)
        self.memory_gauge.pack(side="left", expand=True, pady=10)
        self.disk_gauge = Gauge(gauges, "SYSTEM DRIVE", GREEN)
        self.disk_gauge.pack(side="left", expand=True, pady=10)

        facts = tk.Frame(self.overview, bg=BG)
        facts.pack(fill="both", expand=True)
        self.fact_labels: dict[str, tk.Label] = {}
        entries = (
            ("CPU", CYAN), ("GPU", GREEN), ("MEMORY", PURPLE),
            ("SYSTEM DRIVE", ORANGE), ("MOTHERBOARD", CYAN), ("OPERATING SYSTEM", GREEN),
        )
        for index, (key, color) in enumerate(entries):
            row, column = divmod(index, 2)
            card = self._card(facts)
            card.grid(row=row, column=column, sticky="nsew",
                      padx=(0, 5) if column == 0 else (5, 0),
                      pady=(0, 5) if row < 2 else (5, 0))
            tk.Label(card, text=key, bg=CARD, fg=color,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(9, 2))
            label = tk.Label(card, text="Waiting for data", bg=CARD, fg=TEXT,
                             justify="left", anchor="w", font=("Segoe UI Semibold", 10))
            label.pack(anchor="w", fill="x", padx=14, pady=(0, 9))
            self.fact_labels[key] = label
        for column in range(2):
            facts.columnconfigure(column, weight=1, uniform="facts")
        for row in range(3):
            facts.rowconfigure(row, weight=1, uniform="facts")

    def _build_performance(self) -> None:
        tk.Label(self.performance, text="One-second live samples; Task Manager may use different smoothing.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="e", pady=(0, 6))
        self.cpu_graph = HistoryGraph(self.performance, "CPU LOAD - LIVE HISTORY", CYAN)
        self.cpu_graph.pack(fill="both", expand=True, pady=(0, 6))
        self.memory_graph = HistoryGraph(self.performance, "MEMORY USE - LIVE HISTORY", PURPLE)
        self.memory_graph.pack(fill="both", expand=True, pady=(6, 0))

    def _build_hardware(self) -> None:
        card = self._card(self.hardware_tab)
        card.pack(fill="both", expand=True)
        title = tk.Frame(card, bg=CARD)
        title.pack(fill="x", padx=20, pady=(17, 8))
        tk.Label(title, text="DETECTED HARDWARE", bg=CARD, fg=CYAN,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Label(title, text="Values reported by Windows", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="right")

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
            "Installed memory", "Usable memory", "Motherboard", "BIOS", "Architecture",
            "Operating system", "Uptime", "Battery / power", "Temperatures / fans",
        ):
            self._inventory_row(name)
        self._inventory_row("Fixed drives")

    def _inventory_row(self, name: str) -> None:
        row = tk.Frame(self.inventory, bg=CARD)
        row.pack(fill="x", padx=8, pady=5)
        tk.Label(row, text=name, width=22, anchor="w", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        label = tk.Label(row, text="Unavailable", anchor="w", justify="left",
                         wraplength=690, bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 9))
        label.pack(side="left", fill="x", expand=True)
        self.inventory_labels[name] = label

    def _build_tests(self) -> None:
        card = self._card(self.tests)
        card.pack(fill="both", expand=True)
        tk.Label(card, text="SAFE QUICK CHECK", bg=CARD, fg=TEXT,
                 font=("Segoe UI Semibold", 18)).pack(pady=(34, 7))
        tk.Label(
            card,
            text=("Validates a known SHA-256 calculation and temporary-file integrity.\n"
                  "Throughput is cache-affected and is not a full physical-drive benchmark."),
            bg=CARD, fg=MUTED, justify="center", font=("Segoe UI", 9)
        ).pack()
        self.test_button = tk.Button(
            card, text="RUN QUICK CHECK", command=self.run_tests, bg=CYAN,
            fg="#03121a", activebackground=GREEN, relief="flat", cursor="hand2",
            padx=28, pady=11, font=("Segoe UI", 9, "bold")
        )
        self.test_button.pack(pady=22)
        self.test_output = tk.Text(card, height=10, bg="#070f1e", fg=GREEN,
                                   insertbackground=TEXT, relief="flat", padx=16, pady=13,
                                   font=("Cascadia Mono", 10))
        self.test_output.pack(fill="x", padx=40)
        self._set_test_text("Ready. Click RUN QUICK CHECK to begin.\n")

    def _build_compact(self) -> None:
        self.compact_panel = self._card(self.content)
        top = tk.Frame(self.compact_panel, bg=CARD)
        top.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(top, text="LIVE SYSTEM STATUS", bg=CARD, fg=CYAN,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self.compact_updated = tk.Label(top, text="Waiting", bg=CARD, fg=MUTED,
                                        font=("Segoe UI", 8))
        self.compact_updated.pack(side="right")
        tiles = tk.Frame(self.compact_panel, bg=CARD)
        tiles.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.compact_cpu = MetricTile(tiles, "CPU LOAD", CYAN)
        self.compact_memory = MetricTile(tiles, "MEMORY", PURPLE)
        self.compact_disk = MetricTile(tiles, "SYSTEM DRIVE", GREEN)
        for column, tile in enumerate((self.compact_cpu, self.compact_memory, self.compact_disk)):
            tile.grid(row=0, column=column, sticky="nsew", padx=5)
            tiles.columnconfigure(column, weight=1, uniform="compact")
        tiles.rowconfigure(0, weight=1)

    def _build_footer(self) -> None:
        footer = tk.Frame(self, bg=PANEL, height=34)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        self.updated_label = tk.Label(footer, text="Waiting for first reading", bg=PANEL,
                                      fg=MUTED, font=("Segoe UI", 8))
        self.updated_label.pack(side="left", padx=18)
        github = tk.Label(footer, text="Made by Kieranmcm07  |  GitHub", bg=PANEL,
                          fg=CYAN, cursor="hand2", font=("Segoe UI", 8, "bold"))
        github.pack(side="right", padx=18)
        github.bind("<Button-1>", lambda _e: webbrowser.open_new_tab(GITHUB_URL))

    def _sampler(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.results.put(("snapshot", take_snapshot()))
            except Exception as exc:
                self.results.put(("sensor_error", exc))
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.05, 1.0 - elapsed))

    def _poll_results(self) -> None:
        try:
            while True:
                kind, result = self.results.get_nowait()
                if kind == "snapshot":
                    self._render_snapshot(result)
                elif kind == "tests":
                    self._render_tests(result)
                else:
                    self.live_status.configure(text="!  SENSOR ERROR", fg=RED)
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.after(100, self._poll_results)

    def _render_snapshot(self, snapshot) -> None:
        if self.latest_snapshot and snapshot.captured_at < self.latest_snapshot.captured_at:
            return
        self.latest_snapshot = snapshot
        timestamp = datetime.fromtimestamp(snapshot.captured_at).strftime("%H:%M:%S")
        self.live_status.configure(text="*  LIVE", fg=GREEN)
        self.updated_label.configure(text=f"Updated {timestamp}  |  1 sec refresh")
        self.compact_updated.configure(text=f"Updated {timestamp}")
        self.cpu_gauge.set(snapshot.cpu_usage_percent)
        self.memory_gauge.set(snapshot.memory_used_percent)
        self.disk_gauge.set(snapshot.disk_used_percent)
        self.cpu_graph.add(snapshot.cpu_usage_percent)
        self.memory_graph.add(snapshot.memory_used_percent)

        cores = (f"{snapshot.physical_cores} cores / {snapshot.logical_cpus} threads"
                 if snapshot.physical_cores else f"{snapshot.logical_cpus} logical processors")
        gpu_text = ", ".join(self.hardware.gpu_names) or "No display adapter reported"
        self.fact_labels["CPU"].configure(text=f"{snapshot.processor}  |  {cores}")
        self.fact_labels["GPU"].configure(text=gpu_text)
        self.fact_labels["MEMORY"].configure(
            text=f"{value_text(snapshot.memory_installed_gib, ' GiB')} installed  |  "
                 f"{value_text(snapshot.memory_used_gib, ' GiB')} in use"
        )
        self.fact_labels["SYSTEM DRIVE"].configure(
            text=f"{snapshot.system_drive}  |  {snapshot.disk_free_gib:.1f} GiB free of "
                 f"{snapshot.disk_total_gib:.1f} GiB"
        )
        self.fact_labels["MOTHERBOARD"].configure(text=self.hardware.motherboard)
        self.fact_labels["OPERATING SYSTEM"].configure(text=snapshot.operating_system)

        power = "No battery detected"
        if snapshot.battery_percent is not None:
            state = "AC connected" if snapshot.plugged_in is True else "On battery" if snapshot.plugged_in is False else "Power state unknown"
            power = f"{snapshot.battery_percent}% - {state}"
        drives = "\n".join(
            f"{drive.name}  {drive.free_gib:.1f} GiB free / {drive.total_gib:.1f} GiB  ({drive.used_percent:.1f}% used)"
            for drive in snapshot.drives
        ) or "No fixed drive data available"
        inventory = {
            "Computer": snapshot.computer,
            "CPU": snapshot.processor,
            "CPU topology": cores,
            "Reported CPU clock": (f"{self.hardware.cpu_max_mhz} MHz (Windows registry)"
                                   if self.hardware.cpu_max_mhz else "Unavailable"),
            "Graphics": gpu_text,
            "Installed memory": value_text(snapshot.memory_installed_gib, " GiB"),
            "Usable memory": (f"{value_text(snapshot.memory_total_gib, ' GiB')} total, "
                              f"{value_text(snapshot.memory_available_gib, ' GiB')} available"),
            "Motherboard": self.hardware.motherboard,
            "BIOS": self.hardware.bios_version,
            "Architecture": self.hardware.architecture,
            "Operating system": snapshot.operating_system,
            "Uptime": uptime_text(snapshot.uptime_seconds),
            "Battery / power": power,
            "Temperatures / fans": "Unavailable - requires a trusted hardware sensor provider",
            "Fixed drives": drives,
        }
        for name, value in inventory.items():
            self.inventory_labels[name].configure(text=value)

        self.compact_cpu.set(value_text(snapshot.cpu_usage_percent, "%"), cores)
        self.compact_memory.set(value_text(snapshot.memory_used_percent, "%"),
                                f"{value_text(snapshot.memory_used_gib, ' GiB')} used")
        self.compact_disk.set(value_text(snapshot.disk_used_percent, "%"),
                              f"{snapshot.disk_free_gib:.1f} GiB free on {snapshot.system_drive}")

    def toggle_compact(self) -> None:
        self.compact = not self.compact
        self.attributes("-topmost", self.compact)
        if self.compact:
            self.normal_geometry = self.geometry()
            self.tabs.pack_forget()
            self.compact_panel.pack(fill="both", expand=True, padx=14, pady=14)
            self.minsize(610, 300)
            self.geometry("680x340")
            self.compact_button.configure(text="FULL DASHBOARD")
        else:
            self.compact_panel.pack_forget()
            self.tabs.pack(fill="both", expand=True, padx=16, pady=14)
            self.minsize(860, 640)
            self.geometry(self.normal_geometry)
            self.compact_button.configure(text="COMPACT MODE")

    def run_tests(self) -> None:
        self.test_button.configure(state="disabled", text="CHECKING...")
        self._set_test_text("Checking a known CPU calculation...\nChecking temporary-file integrity...\n")
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self) -> None:
        try:
            self.results.put(("tests", (cpu_self_test(), disk_self_test())))
        except Exception as exc:
            self.results.put(("tests", exc))

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
        self.destroy()


def main() -> None:
    HardwareDashboard().mainloop()


if __name__ == "__main__":
    main()
