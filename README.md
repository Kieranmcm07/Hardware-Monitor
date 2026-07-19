<p align="center">
  <img src="assets/nexus-banner.svg" alt="NEXUS Hardware Monitor" width="100%">
</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-151719?style=for-the-badge&logo=python&logoColor=white">
  <img alt="Windows and Linux supported" src="https://img.shields.io/badge/Windows%20%2F%20Linux-supported-151719?style=for-the-badge">
  <img alt="macOS limited" src="https://img.shields.io/badge/macOS-limited-c92d45?style=for-the-badge">
  <img alt="Core pip dependencies: none" src="https://img.shields.io/badge/core%20pip%20dependencies-none-c92d45?style=for-the-badge">
</p>

NEXUS Hardware Monitor 4.0 is a local desktop dashboard and command-line
snapshot tool for the system readings that matter most: CPU, memory, storage,
battery, network activity, hardware details, processes, sensors, alerts, and
session history. The core monitor uses only Python's standard library and the
information exposed by the operating system.

Windows 10/11 and modern Linux distributions are officially supported.
macOS has a best-effort fallback, but many native readings are unavailable;
see [Compatibility and limitations](#compatibility-and-limitations).

> [!NOTE]
> NEXUS reads local system counters. It does not inspect packet contents,
> upload telemetry, or send hardware data to a remote service.

## What's new in 4.0

- **NEXUS Lab** provides eight focused tools from one searchable control center.
- **Process Explorer** shows read-only per-process CPU and memory use.
- **Alert Center** adds configurable CPU, memory, storage, and temperature
  thresholds with hold times, hysteresis, cooldowns, pause/resume, and a recent
  event log.
- **Sensors & Drive Health** combines optional temperature, fan, power,
  voltage, GPU, and SMART/NVMe health sources without making them a requirement
  for the dashboard.
- **History Vault** stores bounded CPU, memory, storage, temperature, and
  network-rate history in a local SQLite database. Retention is configurable
  from 1 to 365 days and defaults to 30 days.
- **Network Diagnostics** runs an explicit, bounded DNS and reachability check,
  reports latency and jitter, and falls back from ICMP to TCP timing when ICMP
  is unavailable.
- **Benchmark Center** offers short, cancellable CPU, memory-copy, and
  temporary-file checks.
- **Hardware Reports** export allow-listed data as a private local HTML or JSON
  file.
- **Customization Studio** persists alert, history, accent, reduced-motion,
  refresh interval, gaming HUD metric/opacity, and tray preferences.
- An always-on-top **Gaming HUD**, optional system tray controls, and the
  existing compact desktop HUD make live readings available without keeping
  the full dashboard in view.

The main dashboard still includes live overview and performance views,
auto-scaling 60-sample graphs, connected physical-adapter rates, per-volume
storage, hardware inventory, Session Insights, and safe quick checks.

## Requirements

- Python 3.10 or newer
- Tkinter for the graphical interface
- No mandatory third-party Python packages

The CLI does not require Tkinter. On Debian or Ubuntu, install the GUI toolkit
with:

~~~bash
sudo apt update
sudo apt install python3-tk
~~~

You can check whether Tkinter is available without starting NEXUS:

~~~bash
python -m tkinter
~~~

Use python3 instead of python on systems where that is the Python 3 command.

## Run the GUI

Run commands from the repository root so Python can find the
hardware_monitor package.

### Windows

Double-click run_hardware_monitor.bat, or run:

~~~powershell
python -m hardware_monitor.gui
~~~

To avoid leaving a console window behind:

~~~powershell
pythonw -m hardware_monitor.gui
~~~

### Linux

~~~bash
python3 -m hardware_monitor.gui
~~~

The included launcher is equivalent:

~~~bash
chmod +x run_hardware_monitor.sh
./run_hardware_monitor.sh
~~~

### macOS (limited, not officially supported)

~~~bash
python3 -m hardware_monitor.gui
~~~

The window can open when the Python installation includes Tkinter, but macOS
does not currently have native NEXUS collectors for several core readings.

## Command line

The CLI snapshot and quick check need no graphical display:

~~~text
# Human-readable snapshot
python -m hardware_monitor.main

# Raw snapshot JSON
python -m hardware_monitor.main --json

# Human-readable snapshot followed by the CPU and temporary-file quick checks
python -m hardware_monitor.main --test
~~~

On Linux and macOS, replace python with python3 when needed. The --json option
prints the operating-system snapshot and exits; the v4 Lab tools are currently
GUI features.

## Optional integrations

Missing or inaccessible integrations fail closed: the rest of NEXUS continues
to run and reports why that capability is unavailable.

| Integration | Platform | Adds | Installation or requirement |
| --- | --- | --- | --- |
| pystray + Pillow | Windows/Linux; other platforms best effort | System tray icon, menu, and notifications | python -m pip install -r requirements_optional.txt |
| Linux hwmon | Linux | Temperatures, fans, power, and voltage exposed by the kernel | Built in; requires readable /sys/class/hwmon entries |
| LibreHardwareMonitor | Windows | CPU/GPU/board sensor readings | Run its optional local web server at http://127.0.0.1:8085/data.json |
| nvidia-smi | Windows/Linux; driver dependent | NVIDIA temperature, fan, power, load, and memory-use readings | Installed with supported NVIDIA drivers; no Python package is used |
| smartctl from smartmontools | Windows/Linux; other platforms best effort | Read-only SMART and NVMe health | Install smartmontools and ensure smartctl is on PATH |

Only pystray and Pillow belong in requirements_optional.txt. LibreHardwareMonitor
and smartctl are separate applications, while nvidia-smi is supplied by the
NVIDIA driver.

LibreHardwareMonitor data is accepted only from an HTTP loopback address.
NEXUS does not connect to a remote sensor endpoint. SMART queries omit serial
numbers, never start a drive self-test, never change drive settings, and use a
no-wake check for sleeping drives. Some SMART devices still require elevated
permissions, and USB bridges may not expose health data.

## Dashboard, HUDs, and sessions

- **Compact mode:** click COMPACT MODE to switch to a draggable, always-on-top
  desktop HUD. Change its opacity, snap it to the top-right, and use RESTORE or
  Esc to return to the dashboard.
- **Gaming HUD:** shows selected cached metrics without starting another
  collector or creating network traffic. Its metrics and opacity are
  configurable.
- **Session Insights:** records one sample per live refresh, supports
  pause/resume and reset, and exports CSV. The latest 86,400 rows remain
  exportable (about 24 hours at the default one-second refresh), while summary
  totals and peaks continue for the whole session.
- **Persistent history:** history is separate from the in-memory session
  recorder. It stores only numeric performance samples in the local SQLite
  database and prunes according to the configured retention period.

Default v4 alert thresholds are CPU 90%, memory 90%, storage 90%, and
temperature 85 degrees C. A value must remain high for its rule's hold period
before an alert is raised, and it must fall below the hysteresis boundary before
the alert resolves. Alerts are informational warnings, not hardware-health
diagnoses.

## How readings work

Windows readings use native system APIs and the registry. Linux readings use
local sources such as /proc, /sys, mounted filesystems, and kernel counters.

Network rates are calculated from changes in each connected physical adapter's
64-bit byte counters. Virtual-only interfaces such as tunnels, bridges, and
container links are excluded to reduce double counting. Reconnects and counter
resets establish a new baseline instead of creating a false spike.

A few details are important when interpreting results:

- Link speed is the adapter's connection rate to a router, switch, or access
  point; it is not an internet speed test.
- Network totals include local traffic, internet traffic, background
  applications, and VPN overhead on the physical connection.
- Session traffic totals begin when NEXUS starts or when RESET TRAFFIC is
  clicked; they are not lifetime totals.
- Storage uses GiB, where 1 GiB is 1,073,741,824 bytes.
- Sensor, process, battery, firmware, and SMART coverage depends on the
  operating system, permissions, drivers, firmware, and device interfaces.
- Per-process CPU use needs two samples to calculate a rate, so the first
  Process Explorer sample shows N/A for CPU.
- Benchmark scores are short local estimates. Disk results are affected by
  operating-system caching and are not full physical-drive benchmarks.

## Privacy and safety

NEXUS is designed to stay local and read-only:

- There is no analytics, telemetry upload, account, cloud sync, packet capture,
  or remote-control feature.
- Settings, history, session CSV files, and reports remain on the computer.
  CSV files and reports are written only to a path selected by the user.
- Persistent history contains timestamps and numeric usage/rate samples, not
  process names, usernames, IP addresses, packet contents, or device serials.
- HTML/JSON reports explicitly exclude usernames, IP addresses, device paths,
  and serial numbers. Review any generated report before sharing it because it
  still contains system and hardware details.
- Raw CLI JSON intentionally includes the local computer name and live
  snapshot fields. Treat it as local output and review it before sharing.
- The monitor never changes clocks, voltages, fan curves, power limits, network
  configuration, or drive settings.
- Network Diagnostics is the only built-in feature that deliberately contacts
  a user-selected host. It runs only when requested and sends a small, bounded
  ICMP or TCP probe series.
- Benchmarks run only when requested, can be cancelled, use bounded buffers,
  remove their temporary file, and may briefly increase CPU, memory, and disk
  activity.

By default, settings and history are stored in the following per-user
locations:

| Data | Windows | Linux and macOS fallback |
| --- | --- | --- |
| Settings | %APPDATA%\NEXUS Hardware Monitor\settings.json | $XDG_CONFIG_HOME/nexus-hardware-monitor/settings.json, or ~/.config/nexus-hardware-monitor/settings.json |
| History | %LOCALAPPDATA%\NEXUS Hardware Monitor\history.sqlite3 | $XDG_DATA_HOME/nexus-hardware-monitor/history.sqlite3, or ~/.local/share/nexus-hardware-monitor/history.sqlite3 |

## Compatibility and limitations

| Platform | Support level | Expected coverage |
| --- | --- | --- |
| Windows 10/11 | Official | Native CPU, memory, storage, battery, uptime, hardware, physical network counters, and process data; optional LibreHardwareMonitor, NVIDIA, SMART, and tray features |
| Modern Linux | Official | Native /proc and /sys CPU, memory, storage, battery, uptime, hardware, physical network, process, and hwmon data; optional NVIDIA, SMART, and tray features |
| macOS | Limited fallback, not tested or officially supported | The GUI/CLI, generic system identity, root-volume storage, reports, history, settings, benchmarks, and TCP diagnostics may work; native CPU load, memory use, battery, uptime, network-adapter counters, process data, and native sensors are currently unavailable |

On every platform, unavailable readings appear as N/A or an explicit
capability status instead of being fabricated.

Window opacity, transparent/borderless HUD behavior, always-on-top behavior,
tray support, and exact multi-monitor placement depend on the desktop and
window manager. Linux Wayland sessions are especially likely to restrict
positioning or global topmost hints. ICMP diagnostics can also be restricted by
the operating system or local policy; NEXUS labels and uses TCP timing when it
can fall back safely.

## Tests

Run the standard-library test suite from the repository root:

~~~bash
python -m unittest discover -s tests -v
~~~

Use python3 in that command where appropriate. The suite covers native
platform parsers, snapshots and network-rate tracking, recorder retention and
CSV export, settings validation and atomic writes, alert state transitions,
SQLite history, process accounting, optional sensor and SMART parsers,
diagnostic fallbacks, benchmarks and cleanup, privacy-safe reports, theme and
HUD helpers, control-center navigation, and GUI helpers.

The tests mock operating-system and optional-tool boundaries where possible.
Running the test suite does not require LibreHardwareMonitor, smartctl,
nvidia-smi, pystray, or Pillow. Benchmark tests use deliberately small,
temporary workloads and clean up their files.

## Project layout

~~~text
hardware_monitor/
|-- __init__.py          # Package metadata
|-- gui.py               # Main dashboard, compact mode, and integration loop
|-- main.py              # CLI snapshot, JSON, and quick-check entry point
|-- monitor.py           # Native Windows/Linux core collectors
|-- network.py           # Adapter rates, totals, peaks, and formatting
|-- recorder.py          # In-memory Session Insights and CSV export
|-- alerts.py            # Stateful threshold engine
|-- benchmarks.py        # Cancellable CPU, memory, and temporary-file checks
|-- control_center.py    # Searchable NEXUS Lab launcher
|-- diagnostics.py       # DNS, ICMP, and TCP diagnostics
|-- feature_windows.py   # NEXUS Lab tool windows
|-- history.py           # Background SQLite history
|-- overlay.py           # Gaming HUD
|-- processes.py         # Native read-only process collection
|-- report.py            # Privacy-filtered HTML/JSON reports
|-- sensors.py           # hwmon, LibreHardwareMonitor, and NVIDIA providers
|-- settings.py          # Validated atomic settings
|-- smart.py             # Read-only smartctl integration
|-- theme.py             # Semantic themes and layout helpers
`-- tray.py              # Optional pystray adapter

assets/                  # Banner and repository artwork
tests/                   # Standard-library unit tests
requirements_optional.txt
run_hardware_monitor.bat
run_hardware_monitor.sh
~~~

---

Made by [Kieranmcm07](https://github.com/Kieranmcm07). If NEXUS is useful to
you, leaving the repository a star would be appreciated.
