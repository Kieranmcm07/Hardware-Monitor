# NEXUS Hardware Monitor

A live Windows PC dashboard made by [Kieranmcm07](https://github.com/Kieranmcm07).

## New in v3

- Animated neon gauges, live status pulse, scan line, clock, and telemetry banner
- **Session Insights** records one sample per second with average/peak statistics,
  explicit threshold events, pause/reset controls, and CSV export
- **Desktop HUD** is a draggable, always-on-top overlay with CPU/RAM sparklines,
  adjustable opacity, top-right snapping, and a visible restore button
- Responsive fullscreen Overview: metadata stays compact while extra room becomes
  useful live CPU/RAM history instead of stretched empty cards
- Dynamic **Storage** tab showing every fixed drive (C:, D:, E:, and others) with
  individual free/total capacity bars and clearly documented warning thresholds
- Pausable performance graphs with honest straight-line samples

Session alerts are simple documented thresholds—not a hardware-health diagnosis:
CPU or RAM at 85%+, and capacity at 90%+ on any detected fixed drive.

## Start the GUI

Double-click `run_hardware_monitor.bat`, or run:

```powershell
cd "C:\Users\kiera\Documents\Scripts\Python Files\Hardware Monitor"
pythonw -m hardware_monitor.gui
```

Use **COMPACT MODE** to enter the Desktop HUD. Drag its title to move it, choose
70/85/100 opacity, click **RESTORE** (or press Escape) to return to the full
dashboard.

The dashboard reports Windows-provided CPU, graphics, memory, motherboard,
BIOS, drive, battery, and operating-system information. Capacity is displayed
in GiB (1 GiB = 1,073,741,824 bytes). Temperature, voltage, power, and fan RPM
are deliberately shown as unavailable unless a trusted hardware sensor provider
is added; Windows does not expose those readings reliably on every PC.

The quick check verifies CPU calculation and temporary-file integrity. Its small,
cache-affected throughput figure is not a full physical-drive benchmark.

## Command line

```powershell
python -m hardware_monitor.main
python -m hardware_monitor.main --test
python -m hardware_monitor.main --json
python -m unittest discover -s tests -v
```
