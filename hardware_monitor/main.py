from __future__ import annotations

import argparse
import json

from hardware_monitor.monitor import cpu_self_test, disk_self_test, take_snapshot


def show(value: object, suffix: str = "") -> str:
    return "Not available" if value is None else f"{value}{suffix}"


def print_dashboard() -> None:
    info = take_snapshot()
    core_text = (f"{info.physical_cores} physical / {info.logical_cpus} logical"
                 if info.physical_cores else f"{info.logical_cpus} logical")
    power = "No battery detected"
    if info.battery_percent is not None:
        state = "AC connected" if info.plugged_in else "On battery"
        power = f"{info.battery_percent}% ({state})"
    print("\nNEXUS HARDWARE MONITOR")
    print("=" * 58)
    print(f"Computer         : {info.computer}")
    print(f"Operating system : {info.operating_system}")
    print(f"Processor        : {info.processor}")
    print(f"CPU topology     : {core_text}")
    print(f"CPU usage        : {show(info.cpu_usage_percent, '%')}")
    print(f"Installed memory : {show(info.memory_installed_gib, ' GiB')}")
    print(f"Usable memory    : {show(info.memory_total_gib, ' GiB')}")
    print(f"Memory in use    : {show(info.memory_used_gib, ' GiB')} ({show(info.memory_used_percent, '%')})")
    print(f"System drive     : {info.system_drive}")
    print(f"Drive capacity   : {info.disk_total_gib} GiB")
    print(f"Drive free       : {info.disk_free_gib} GiB")
    print(f"Drive used       : {info.disk_used_percent}%")
    print(f"Battery / power  : {power}")
    print("Temperatures     : Not available without a hardware sensor provider")
    print("\nMade by Kieranmcm07 - https://github.com/Kieranmcm07")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and safely test this PC's hardware.")
    parser.add_argument("--test", action="store_true", help="run quick CPU and file-integrity checks")
    parser.add_argument("--json", action="store_true", help="output a snapshot as JSON")
    args = parser.parse_args()
    if args.json:
        print(json.dumps(take_snapshot().as_dict(), indent=2))
        return
    print_dashboard()
    if args.test:
        print("\nQUICK CHECK")
        print("=" * 58)
        print("CPU calculation:", json.dumps(cpu_self_test()))
        print("File integrity :", json.dumps(disk_self_test()))


if __name__ == "__main__":
    main()
