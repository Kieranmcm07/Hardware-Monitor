#   Made by Kieranmcm07 on GitHub
#   GitHub: https://github.com/Kieranmcm07

from __future__ import annotations

from dataclasses import dataclass

from hardware_monitor.monitor import NetworkInterfaceInfo


@dataclass(frozen=True)
class InterfaceRate:
    luid: int
    alias: str
    description: str
    kind: str
    receive_link_bps: int
    transmit_link_bps: int
    download_bps: float
    upload_bps: float


@dataclass(frozen=True)
class NetworkRates:
    download_bps: float
    upload_bps: float
    session_received_bytes: int
    session_sent_bytes: int
    peak_download_bps: float
    peak_upload_bps: float
    adapters: tuple[InterfaceRate, ...]


class NetworkRateTracker:
    """Turns monotonic 64-bit interface counters into rates and session totals."""

    def __init__(self) -> None:
        self._previous: dict[int, tuple[int, int, float]] = {}
        self._session_received = 0
        self._session_sent = 0
        self._peak_download = 0.0
        self._peak_upload = 0.0
        self._accept_after_monotonic: float | None = None

    def update(
        self, interfaces: tuple[NetworkInterfaceInfo, ...], monotonic_at: float
    ) -> NetworkRates:
        if (
            self._accept_after_monotonic is not None
            and monotonic_at < self._accept_after_monotonic
        ):
            # A poll already in progress when Reset was clicked must not become
            # the new baseline or it would re-add a slice of pre-reset traffic.
            return NetworkRates(
                download_bps=0.0,
                upload_bps=0.0,
                session_received_bytes=self._session_received,
                session_sent_bytes=self._session_sent,
                peak_download_bps=self._peak_download,
                peak_upload_bps=self._peak_upload,
                adapters=tuple(InterfaceRate(
                    luid=interface.luid,
                    alias=interface.alias,
                    description=interface.description,
                    kind=interface.kind,
                    receive_link_bps=interface.receive_link_bps,
                    transmit_link_bps=interface.transmit_link_bps,
                    download_bps=0.0,
                    upload_bps=0.0,
                ) for interface in interfaces),
            )
        self._accept_after_monotonic = None
        new_previous: dict[int, tuple[int, int, float]] = {}
        adapter_rates: list[InterfaceRate] = []
        received_delta_total = 0
        sent_delta_total = 0
        for interface in interfaces:
            download_bps = 0.0
            upload_bps = 0.0
            previous = self._previous.get(interface.luid)
            if previous is not None:
                previous_received, previous_sent, previous_time = previous
                elapsed = monotonic_at - previous_time
                if elapsed > 0:
                    received_delta = interface.received_bytes - previous_received
                    sent_delta = interface.sent_bytes - previous_sent
                    # A negative 64-bit delta means the adapter reset/reconnected.
                    if received_delta >= 0:
                        received_delta_total += received_delta
                        download_bps = received_delta / elapsed
                    if sent_delta >= 0:
                        sent_delta_total += sent_delta
                        upload_bps = sent_delta / elapsed
            new_previous[interface.luid] = (
                interface.received_bytes,
                interface.sent_bytes,
                monotonic_at,
            )
            adapter_rates.append(InterfaceRate(
                luid=interface.luid,
                alias=interface.alias,
                description=interface.description,
                kind=interface.kind,
                receive_link_bps=interface.receive_link_bps,
                transmit_link_bps=interface.transmit_link_bps,
                download_bps=download_bps,
                upload_bps=upload_bps,
            ))
        self._previous = new_previous
        self._session_received += received_delta_total
        self._session_sent += sent_delta_total
        download_total = sum(adapter.download_bps for adapter in adapter_rates)
        upload_total = sum(adapter.upload_bps for adapter in adapter_rates)
        self._peak_download = max(self._peak_download, download_total)
        self._peak_upload = max(self._peak_upload, upload_total)
        return NetworkRates(
            download_bps=download_total,
            upload_bps=upload_total,
            session_received_bytes=self._session_received,
            session_sent_bytes=self._session_sent,
            peak_download_bps=self._peak_download,
            peak_upload_bps=self._peak_upload,
            adapters=tuple(adapter_rates),
        )

    def reset_session(self, monotonic_at: float | None = None) -> None:
        self._session_received = 0
        self._session_sent = 0
        self._peak_download = 0.0
        self._peak_upload = 0.0
        # The next poll becomes a fresh baseline. Otherwise its delta would
        # include bytes transferred before the user clicked Reset Traffic.
        self._previous.clear()
        self._accept_after_monotonic = monotonic_at


def format_bytes(value: float | int) -> str:
    amount = max(0.0, float(value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    decimals = 0 if amount >= 100 else 1
    return f"{amount:.{decimals}f} {unit}"


def format_rate(bytes_per_second: float | int) -> str:
    return f"{format_bytes(bytes_per_second)}/s"


def format_link_speed(bits_per_second: int) -> str:
    speed = max(0, int(bits_per_second))
    if speed >= 1_000_000_000:
        return f"{speed / 1_000_000_000:.1f} Gbps"
    if speed >= 1_000_000:
        return f"{speed / 1_000_000:.0f} Mbps"
    if speed >= 1_000:
        return f"{speed / 1_000:.0f} Kbps"
    return f"{speed} bps" if speed else "Unknown"
