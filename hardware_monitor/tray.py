"""Optional system-tray adapter with a queue-only GUI boundary.

``pystray`` and Pillow are imported only by ``start``.  Tray callbacks never
touch Tk/Qt widgets; they enqueue commands for the application's main thread.
"""

from __future__ import annotations

import importlib
import queue
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class TrayCapability(str, Enum):
    READY = "ready"
    RUNNING = "running"
    MISSING = "missing"
    ERROR = "error"
    STOPPED = "stopped"


class TrayCommand(str, Enum):
    SHOW = "show"
    HIDE = "hide"
    TOGGLE_HUD = "toggle_hud"
    TOGGLE_ALERTS = "toggle_alerts"
    EXIT = "exit"


@dataclass(frozen=True)
class TrayStatus:
    capability: TrayCapability
    detail: str = ""


class TrayController:
    """Own a pystray backend thread and expose commands through a queue."""

    def __init__(
        self,
        commands: queue.Queue[TrayCommand] | None = None,
        *,
        importer: Callable[[str], Any] = importlib.import_module,
    ) -> None:
        self.commands: queue.Queue[TrayCommand] = commands or queue.Queue()
        self.importer = importer
        self._lock = threading.RLock()
        self._status = TrayStatus(TrayCapability.READY)
        self._icon: Any | None = None
        self._thread: threading.Thread | None = None

    @property
    def status(self) -> TrayStatus:
        with self._lock:
            return self._status

    def _set_status(self, capability: TrayCapability, detail: str = "") -> TrayStatus:
        with self._lock:
            self._status = TrayStatus(capability, detail)
            return self._status

    def _enqueue(self, command: TrayCommand) -> None:
        self.commands.put_nowait(command)

    def _callback(self, command: TrayCommand) -> Callable[..., None]:
        def callback(*_args: Any, **_kwargs: Any) -> None:
            self._enqueue(command)

        return callback

    @staticmethod
    def _create_image(image_module: Any, draw_module: Any) -> Any:
        image = image_module.new("RGBA", (64, 64), (8, 8, 10, 255))
        draw = draw_module.Draw(image)
        draw.rounded_rectangle(
            (3, 3, 60, 60), radius=15, fill=(8, 8, 10, 255), outline=(235, 235, 238, 255), width=4
        )
        draw.line((18, 45, 18, 19, 46, 45, 46, 19), fill=(224, 35, 52, 255), width=7, joint="curve")
        draw.ellipse((49, 8, 57, 16), fill=(224, 35, 52, 255))
        return image

    def start(self) -> TrayStatus:
        """Start once.  Returns a capability result instead of raising."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._status
        try:
            pystray = self.importer("pystray")
            image_module = self.importer("PIL.Image")
            draw_module = self.importer("PIL.ImageDraw")
        except ModuleNotFoundError:
            return self._set_status(
                TrayCapability.MISSING,
                "System tray support requires optional packages pystray and Pillow.",
            )
        except Exception as exc:
            return self._set_status(
                TrayCapability.ERROR, f"Tray dependencies could not be loaded: {exc}"
            )

        try:
            menu = pystray.Menu(
                pystray.MenuItem("Show NEXUS", self._callback(TrayCommand.SHOW), default=True),
                pystray.MenuItem("Hide window", self._callback(TrayCommand.HIDE)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Toggle gaming HUD", self._callback(TrayCommand.TOGGLE_HUD)),
                pystray.MenuItem("Pause / resume alerts", self._callback(TrayCommand.TOGGLE_ALERTS)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self._callback(TrayCommand.EXIT)),
            )
            icon = pystray.Icon(
                "nexus-hardware-monitor",
                self._create_image(image_module, draw_module),
                "NEXUS Hardware Monitor",
                menu,
            )
        except Exception as exc:
            return self._set_status(TrayCapability.ERROR, f"Tray could not be created: {exc}")

        def run_backend() -> None:
            self._set_status(TrayCapability.RUNNING)
            try:
                icon.run()
            except Exception as exc:
                self._set_status(TrayCapability.ERROR, f"Tray backend stopped: {exc}")
            else:
                if self.status.capability is not TrayCapability.ERROR:
                    self._set_status(TrayCapability.STOPPED)

        thread = threading.Thread(
            target=run_backend,
            name="nexus-system-tray",
            daemon=True,
        )
        with self._lock:
            self._icon = icon
            self._thread = thread
            self._status = TrayStatus(TrayCapability.READY, "Tray backend is starting.")
        thread.start()
        return self.status

    def notify(self, message: str, title: str = "NEXUS Hardware Monitor") -> bool:
        with self._lock:
            icon = self._icon
        if icon is None or self.status.capability is not TrayCapability.RUNNING:
            return False
        try:
            icon.notify(str(message)[:1_000], str(title)[:120])
            return True
        except Exception:
            return False

    def drain_commands(self, limit: int = 32) -> tuple[TrayCommand, ...]:
        commands: list[TrayCommand] = []
        for _ in range(max(0, min(int(limit), 256))):
            try:
                commands.append(self.commands.get_nowait())
            except queue.Empty:
                break
        return tuple(commands)

    def stop(self, join_timeout: float = 1.0) -> TrayStatus:
        with self._lock:
            icon = self._icon
            thread = self._thread
        if icon is not None:
            try:
                icon.stop()
            except Exception as exc:
                return self._set_status(TrayCapability.ERROR, f"Tray could not stop: {exc}")
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, min(float(join_timeout), 5.0)))
        with self._lock:
            self._icon = None
            self._thread = None
        return self._set_status(TrayCapability.STOPPED)


__all__ = [
    "TrayCapability",
    "TrayCommand",
    "TrayController",
    "TrayStatus",
]
