from __future__ import annotations

import argparse
import json

from hardware_monitor.monitor import cpu_self_test, disk_self_test, take_snapshot
from hardware_monitor.network import format_bytes, format_link_speed


def show(value: object, suffix: str = "") -> str:
    return "Not available" if value is None else f"{value}{suffix}"


def power_status(battery_percent: int | None, plugged_in: bool | None) -> str:
    if battery_percent is None:
        return "No battery detected"
    if plugged_in is True:
        state = "AC connected"
    elif plugged_in is False:
        state = "On battery"
    else:
        state = "Power source unknown"
    return f"{battery_percent}% ({state})"


def print_dashboard() -> None:
    info = take_snapshot()
    core_text = (f"{info.physical_cores} physical / {info.logical_cpus} logical"
                 if info.physical_cores else f"{info.logical_cpus} logical")
    power = power_status(info.battery_percent, info.plugged_in)
    print("\nNEXUS HARDWARE MONITOR")
    print("=" * 58)
    print(f"Computer         : {info.computer}")
    print(f"Operating system : {info.operating_system}")
    print(f"Processor        : {info.processor}")
    print(f"CPU topology     : {core_text}")
    print(f"CPU usage        : {show(info.cpu_usage_percent, '%')}")
    print(f"Reported memory  : {show(info.memory_installed_gib, ' GiB')}")
    print(f"Usable memory    : {show(info.memory_total_gib, ' GiB')}")
    print(f"Memory in use    : {show(info.memory_used_gib, ' GiB')} ({show(info.memory_used_percent, '%')})")
    print(f"System volume    : {info.system_drive}")
    print(f"Volume capacity  : {info.disk_total_gib} GiB")
    print(f"Volume free      : {info.disk_free_gib} GiB")
    print(f"Volume used      : {info.disk_used_percent}%")
    print(f"Battery / power  : {power}")
    print("Temperatures     : Not available without a hardware sensor provider")
    print("\nCONNECTED PHYSICAL NETWORK ADAPTERS")
    print("-" * 58)
    if not info.network_interfaces:
        print("No connected physical network adapters detected")
    for adapter in info.network_interfaces:
        print(f"{adapter.alias} ({adapter.kind})")
        receive_link = format_link_speed(adapter.receive_link_bps)
        transmit_link = format_link_speed(adapter.transmit_link_bps)
        link_speed = (
            receive_link if receive_link == transmit_link
            else f"{receive_link} receive / {transmit_link} transmit"
        )
        print(f"  Link speed     : {link_speed}")
        print(f"  System totals  : {format_bytes(adapter.received_bytes)} received / "
              f"{format_bytes(adapter.sent_bytes)} sent")
    print("\nMade by Kieranmcm07 - https://github.com/Kieranmcm07")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect and safely test this computer's hardware."
    )
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
