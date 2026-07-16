# NEXUS Hardware Monitor

A live Windows PC dashboard made by [Kieranmcm07](https://github.com/Kieranmcm07).

## New in v3.2

- A live **Network** tab shows download/upload rates, session totals and peaks,
  auto-scaled 60-second graphs, and a card for every connected physical adapter
- Network adapter cards show the Windows adapter name, connection type, current
  link speed, and separate receive/transmit activity
- Session CSV exports now include the live network rate and session byte totals
- The **Storage** tab scrolls when a PC has several fixed drives, so C:, D:, and
  any additional fixed drives remain accessible in windowed and fullscreen layouts
- More robust live sampling: stale frames are discarded, sensor errors change
  the LIVE indicator, pauses no longer count toward session duration, and missing
  graph readings are displayed as gaps

## Dashboard features

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

Session alerts are simple documented thresholds - not a hardware-health diagnosis:
CPU or RAM at 85%+, and capacity at 90%+ on any detected fixed drive.

CSV export retains the latest 86,400 recorded samples (about 24 hours at the
normal refresh rate). The on-screen session totals continue for the full session
even after older export rows roll off.

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
BIOS, drive, battery, network, and operating-system information. Capacity is
displayed in GiB (1 GiB = 1,073,741,824 bytes). Temperature, voltage, power, and
fan RPM are deliberately shown as unavailable unless a trusted hardware sensor
provider is added; Windows does not expose those readings reliably on every PC.

### Understanding the network readings

NEXUS reads the 64-bit byte counters Windows maintains for each connected
physical adapter, then calculates rates from the change between one-second
samples. It does not inspect packets or send data anywhere.

- **Link speed** is the adapter's negotiated connection to the router, switch, or
  access point. It is not an internet speed test or a promised download speed.
- Download/upload includes all traffic through the adapter, such as local-network
  transfers, internet traffic, VPN overhead, Windows services, and other apps.
- Very short bursts between refreshes are averaged into the next reading. An
  adapter reconnect or Windows counter reset starts a fresh baseline instead of
  reporting a false spike.
- Session totals begin after NEXUS receives its first sample; they are not the
  machine's lifetime usage totals.

The quick check verifies CPU calculation and temporary-file integrity. Its small,
cache-affected throughput figure is not a full physical-drive benchmark.

## Command line

```powershell
python -m hardware_monitor.main
python -m hardware_monitor.main --test
python -m hardware_monitor.main --json
python -m unittest discover -v
python -m unittest discover -s tests -v
```
