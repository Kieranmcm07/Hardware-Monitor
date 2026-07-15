# NEXUS Hardware Monitor

A live Windows PC dashboard made by [Kieranmcm07](https://github.com/Kieranmcm07).

## Start the GUI

Double-click `run_hardware_monitor.bat`, or run:

```powershell
cd "C:\Users\kiera\Documents\Scripts\Python Files\Hardware Monitor"
pythonw -m hardware_monitor.gui
```

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
