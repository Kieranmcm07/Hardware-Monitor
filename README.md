# NEXUS Hardware Monitor

A live Windows and Linux hardware dashboard made by
[Kieranmcm07](https://github.com/Kieranmcm07).

## New in v3.4

- New graphite black-and-white interface with restrained red focus and alert accents
- True Canvas-drawn rounded tabs, buttons, main panels, drive cards, adapter cards,
  gauges, graphs, metric tiles, and Desktop HUD cells
- Stronger two- and three-pixel visual hierarchy for borders, gauges, graphs,
  progress tracks, selected tabs, and keyboard focus
- Runtime font selection uses modern Windows fonts when installed and portable
  Inter, Noto Sans, DejaVu Sans, and DejaVu Sans Mono fallbacks on Linux
- A single capped animation clock drives graph sweeps, gauge and sparkline halos,
  live-status ripples, metric update pulses, and the red header scanner
- Smooth drive-capacity transitions and animated rounded-button hover, press,
  keyboard-focus, and selected-tab states
- Ambient animation pauses for hidden tabs and minimized windows so the monitor
  does not add unnecessary load to the computer it is measuring

## Added in v3.3

- First-class Linux support using information exposed by `/proc`, `/sys`, and
  the local operating system
- Cross-platform CPU, memory, storage, uptime, battery, and network telemetry
  without a third-party Python package
- Linux physical network adapters use the same live rate graphs, peaks, session totals,
  and per-adapter cards as Windows
- Storage cards support Windows drive letters and Linux mount points
- The Desktop HUD uses the virtual desktop reported by Tk outside Windows and
  safely ignores optional window-manager hints that are unavailable
- Platform-neutral dashboard and command-line labels accurately describe where
  readings come from
- Responsive metric tiles, graph surfaces, and readable long Linux mount paths

## Dashboard features

- Animated gauges, live-status ripple, red scanner, clock, and telemetry banner
- A live **Network** tab with download/upload rates, session totals and peaks,
  auto-scaled 60-second graphs, and connected-adapter details
- **Session Insights** records one sample per second with average/peak statistics,
  explicit threshold events, pause/reset controls, and CSV export
- A draggable, always-on-top **Desktop HUD** with CPU/RAM sparklines,
  adjustable opacity, top-right snapping, and a visible restore button
- A responsive Overview and a scrollable **Storage** tab for multiple volumes
- Pausable performance graphs that show missing readings as gaps
- Built-in CPU-calculation and temporary-file integrity checks

Session alerts are documented information thresholds, not a hardware-health
diagnosis: CPU or RAM at 85%+, and storage capacity at 90%+ on any detected
volume.

CSV export retains the latest 86,400 recorded samples (about 24 hours at the
normal refresh rate). On-screen totals continue for the full session after older
export rows roll off.

## Requirements

- Python 3.10 or newer
- Tkinter and a graphical desktop for the GUI
- Windows 10/11 or a modern Linux distribution

Tkinter is normally included with the Windows Python installer. On
Debian/Ubuntu Linux, install it if needed:

```bash
sudo apt update
sudo apt install python3-tk
```

The command-line snapshot and self-test can still run without opening the GUI.

## Start on Windows

Double-click `run_hardware_monitor.bat`, or use PowerShell:

```powershell
cd "C:\Users\kiera\Documents\Scripts\Python Files\Hardware Monitor"
python -m hardware_monitor.gui
```

`pythonw -m hardware_monitor.gui` also starts it without keeping a console window
open.

## Start on Linux

From the project directory, run:

```bash
python3 -m hardware_monitor.gui
```

Or make the included launcher executable once and use it afterward:

```bash
chmod +x run_hardware_monitor.sh
./run_hardware_monitor.sh
```

Use **COMPACT MODE** to enter the Desktop HUD. Drag its title to move it, choose
70/85/100 opacity, then click **RESTORE** or press Escape to return to the full
dashboard.

## Understanding the readings

Capacity is displayed in GiB (1 GiB = 1,073,741,824 bytes). Windows uses native
system APIs and the registry where appropriate. Linux reads standard kernel and
system files, including `/proc` and `/sys`. A field displays as unavailable when
the operating system does not expose it or the current user cannot read it.

NEXUS reads the operating system's 64-bit byte counters for each connected
physical adapter and calculates rates from the change between samples. It does
not inspect packets or send data anywhere. Virtual-only interfaces such as TUN,
TAP, bridges, and container links are intentionally excluded to avoid counting
the same transfer twice.

- **Link speed** is the adapter's connection to a router, switch, or access point;
  it is not an internet speed test or a promised download speed.
- Download/upload includes local-network transfers, internet traffic, VPN
  overhead on the underlying physical link, background services, and other apps
  using the adapter.
- Short bursts between refreshes are averaged into the next reading. Reconnects
  and counter resets start a fresh baseline instead of producing a false spike.
- Session totals begin with the first NEXUS sample; they are not lifetime totals.

The quick check verifies CPU calculation and temporary-file integrity. Its small,
cache-affected throughput figure is not a full physical-drive benchmark.

## Compatibility and limitations

- Windows and Linux are the supported platforms. macOS and other
  systems may launch through generic fallbacks, but they are not officially
  supported yet and more readings may be unavailable.
- GPU, motherboard, BIOS, physical-memory, battery, and link-speed details depend
  on what the operating system and hardware expose. Linux permissions, virtual
  machines, containers, and WSL can reduce the available details. Under WSL,
  readings describe the Linux environment rather than the full Windows host.
- Temperature, voltage, power, and fan RPM stay unavailable until a trusted
  cross-platform sensor provider is added.
- Always-on-top, opacity, borderless mode, and exact multi-monitor HUD placement
  are window-manager features. Results can vary between X11, Wayland, and Linux
  desktop environments, but the full dashboard remains usable.
- A Linux GUI session needs a working display (`DISPLAY` or the desktop's Wayland
  bridge). Headless servers can use the command-line modes below.

## Command line and tests

Windows:

```powershell
python -m hardware_monitor.main
python -m hardware_monitor.main --test
python -m hardware_monitor.main --json
python -m unittest discover -v
```

Linux:

```bash
python3 -m hardware_monitor.main
python3 -m hardware_monitor.main --test
python3 -m hardware_monitor.main --json
python3 -m unittest discover -v
```
