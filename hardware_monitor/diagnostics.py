"""Bounded, user-triggered network diagnostics for NEXUS v4.

The public ``run_diagnostics`` function performs DNS resolution followed by a
small ICMP series when the operating system permits it.  It transparently
falls back to TCP connection timing when ICMP is unavailable, and labels that
fallback honestly: TCP failures are not presented as ICMP packet loss.
"""

from __future__ import annotations

import ctypes
import ipaddress
import math
import re
import socket
import statistics
import struct
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol, Sequence


class ProbeMethod(str, Enum):
    ICMP = "icmp"
    TCP = "tcp"


class DiagnosticState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    INVALID_TARGET = "invalid_target"
    DNS_FAILED = "dns_failed"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class ResolvedAddress:
    family: int
    address: str


@dataclass(frozen=True)
class Resolution:
    target: str
    addresses: tuple[ResolvedAddress, ...]
    dns_ms: float


@dataclass(frozen=True)
class ProbeSample:
    sequence: int
    method: ProbeMethod
    address: str
    latency_ms: float | None
    error: str = ""


@dataclass(frozen=True)
class DiagnosticResult:
    state: DiagnosticState
    target: str
    addresses: tuple[ResolvedAddress, ...] = ()
    dns_ms: float | None = None
    method: ProbeMethod | None = None
    samples: tuple[ProbeSample, ...] = ()
    sent: int = 0
    received: int = 0
    failures: int = 0
    packet_loss_percent: float | None = None
    failure_percent: float | None = None
    minimum_ms: float | None = None
    average_ms: float | None = None
    maximum_ms: float | None = None
    jitter_ms: float | None = None
    detail: str = ""


class ProbeUnavailable(RuntimeError):
    """Raised when a transport cannot be used on this system."""


class ProbeTransport(Protocol):
    method: ProbeMethod

    def probe(self, address: str, timeout: float, sequence: int) -> float:
        ...


_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def normalize_target(target: str) -> str:
    """Return a normalized IP/IDNA host, or raise ``ValueError``."""

    if not isinstance(target, str):
        raise ValueError("Target must be text.")
    value = target.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if (
        not value
        or len(value) > 253
        or "\x00" in value
        or any(character.isspace() for character in value)
        or "://" in value
        or value.startswith("-")
    ):
        raise ValueError("Enter an IP address or hostname, not a command or URL.")
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        pass
    value = value.rstrip(".")
    try:
        ascii_host = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Hostname is not valid IDNA text.") from exc
    if not ascii_host or any(not _HOST_LABEL.fullmatch(label) for label in ascii_host.split(".")):
        raise ValueError("Hostname contains an invalid label.")
    return ascii_host.casefold()


def resolve_target(
    target: str,
    *,
    resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    clock: Callable[[], float] = time.perf_counter,
) -> Resolution:
    normalized = normalize_target(target)
    started = clock()
    results = resolver(normalized, None, type=socket.SOCK_STREAM)
    dns_ms = max(0.0, (clock() - started) * 1_000.0)
    addresses: list[ResolvedAddress] = []
    seen: set[tuple[int, str]] = set()
    for entry in results:
        if len(entry) < 5:
            continue
        family, sockaddr = entry[0], entry[4]
        if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
            continue
        address = str(sockaddr[0])
        key = (family, address)
        if key not in seen:
            seen.add(key)
            addresses.append(ResolvedAddress(family, address))
    if not addresses:
        raise socket.gaierror("No IPv4 or IPv6 addresses were returned.")
    return Resolution(normalized, tuple(addresses), dns_ms)


def internet_checksum(payload: bytes) -> int:
    if len(payload) % 2:
        payload += b"\x00"
    total = sum(struct.unpack(f"!{len(payload) // 2}H", payload))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


class LinuxIcmpTransport:
    method = ProbeMethod.ICMP

    def __init__(
        self,
        *,
        socket_factory: Callable[..., Any] = socket.socket,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.socket_factory = socket_factory
        self.clock = clock

    def probe(self, address: str, timeout: float, sequence: int) -> float:
        try:
            ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError as exc:
            raise ProbeUnavailable("Linux ICMP transport currently requires IPv4.") from exc
        payload = struct.pack("!d", self.clock()) + b"NEXUSv4"
        header = struct.pack("!BBHHH", 8, 0, 0, 0, sequence & 0xFFFF)
        checksum = internet_checksum(header + payload)
        packet = struct.pack("!BBHHH", 8, 0, checksum, 0, sequence & 0xFFFF) + payload
        try:
            sock = self.socket_factory(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        except PermissionError as exc:
            raise ProbeUnavailable(
                "ICMP is not permitted. Configure Linux ping_group_range or use TCP timing."
            ) from exc
        except OSError as exc:
            raise ProbeUnavailable(f"ICMP socket is unavailable: {exc}") from exc
        try:
            sock.settimeout(timeout)
            started = self.clock()
            sock.sendto(packet, (address, 0))
            reply, _ = sock.recvfrom(65_535)
            elapsed = (self.clock() - started) * 1_000.0
        except socket.timeout as exc:
            raise TimeoutError("ICMP request timed out.") from exc
        finally:
            sock.close()
        offset = 20 if len(reply) >= 28 and (reply[0] >> 4) == 4 else 0
        if len(reply) < offset + 8:
            raise OSError("ICMP reply was truncated.")
        reply_type, _code, _sum, _identifier, reply_sequence = struct.unpack(
            "!BBHHH", reply[offset : offset + 8]
        )
        if reply_type != 0 or reply_sequence != (sequence & 0xFFFF):
            raise OSError("Unexpected ICMP reply.")
        return max(0.0, elapsed)


class _IpOptionInformation(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.c_void_p),
    ]


class _IcmpEchoReply(ctypes.Structure):
    _fields_ = [
        ("Address", ctypes.c_uint32),
        ("Status", ctypes.c_uint32),
        ("RoundTripTime", ctypes.c_uint32),
        ("DataSize", ctypes.c_uint16),
        ("Reserved", ctypes.c_uint16),
        ("Data", ctypes.c_void_p),
        ("Options", _IpOptionInformation),
    ]


class WindowsIcmpTransport:
    method = ProbeMethod.ICMP

    def __init__(self, *, library: Any | None = None) -> None:
        self.library = library

    def _api(self) -> Any:
        if not sys.platform.startswith("win") and self.library is None:
            raise ProbeUnavailable("Windows ICMP API is not available on this platform.")
        if self.library is None:
            try:
                self.library = ctypes.WinDLL("iphlpapi.dll")
            except (AttributeError, OSError) as exc:
                raise ProbeUnavailable("Windows ICMP API could not be loaded.") from exc
        api = self.library
        # ctypes defaults function returns to 32-bit integers. Explicit handle
        # signatures are required on 64-bit Windows or the ICMP handle is
        # truncated and IcmpSendEcho can write through an invalid pointer.
        try:
            api.IcmpCreateFile.argtypes = ()
            api.IcmpCreateFile.restype = ctypes.c_void_p
            api.IcmpSendEcho.argtypes = (
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint16,
                ctypes.POINTER(_IpOptionInformation),
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
            )
            api.IcmpSendEcho.restype = ctypes.c_uint32
            api.IcmpCloseHandle.argtypes = (ctypes.c_void_p,)
            api.IcmpCloseHandle.restype = ctypes.c_int
        except (AttributeError, TypeError):
            # Injectable pure-Python fakes used by tests do not need signatures.
            pass
        return api

    def probe(self, address: str, timeout: float, sequence: int) -> float:
        try:
            packed_address = socket.inet_aton(address)
        except OSError as exc:
            raise ProbeUnavailable("Windows ICMP transport currently requires IPv4.") from exc
        api = self._api()
        handle = api.IcmpCreateFile()
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in {0, None, invalid_handle}:
            raise ProbeUnavailable("Windows could not create an ICMP handle.")
        payload = f"NEXUSv4:{sequence}".encode("ascii")
        request_buffer = ctypes.create_string_buffer(payload)
        reply_buffer = ctypes.create_string_buffer(8_192)
        destination = struct.unpack("=I", packed_address)[0]
        try:
            replies = api.IcmpSendEcho(
                handle,
                destination,
                request_buffer,
                len(payload),
                None,
                reply_buffer,
                len(reply_buffer),
                max(1, int(timeout * 1_000)),
            )
            if not replies:
                raise TimeoutError("ICMP request timed out or failed.")
            reply = _IcmpEchoReply.from_buffer(reply_buffer)
            if reply.Status != 0:
                raise OSError(f"Windows ICMP status {reply.Status}.")
            return float(reply.RoundTripTime)
        finally:
            api.IcmpCloseHandle(handle)


class TcpConnectTransport:
    method = ProbeMethod.TCP

    def __init__(
        self,
        family: int,
        port: int = 443,
        *,
        socket_factory: Callable[..., Any] = socket.socket,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not 1 <= int(port) <= 65_535:
            raise ValueError("TCP port must be between 1 and 65535.")
        self.family = family
        self.port = int(port)
        self.socket_factory = socket_factory
        self.clock = clock

    def probe(self, address: str, timeout: float, sequence: int) -> float:
        sock = self.socket_factory(self.family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            started = self.clock()
            destination: Any = (address, self.port, 0, 0) if self.family == socket.AF_INET6 else (address, self.port)
            sock.connect(destination)
            return max(0.0, (self.clock() - started) * 1_000.0)
        except socket.timeout as exc:
            raise TimeoutError("TCP connection timed out.") from exc
        finally:
            sock.close()


def summarize_samples(
    target: str,
    resolution: Resolution,
    samples: Sequence[ProbeSample],
    *,
    state: DiagnosticState | None = None,
    detail: str = "",
) -> DiagnosticResult:
    sent = len(samples)
    values = [sample.latency_ms for sample in samples if sample.latency_ms is not None]
    received = len(values)
    failures = sent - received
    method = samples[0].method if samples else None
    failure_percent = failures / sent * 100.0 if sent else None
    packet_loss = failure_percent if method is ProbeMethod.ICMP else None
    if state is None:
        if sent and failures == 0:
            state = DiagnosticState.COMPLETE
        elif received:
            state = DiagnosticState.PARTIAL
        else:
            state = DiagnosticState.ERROR

    def rounded(value: float | None) -> float | None:
        return round(value, 3) if value is not None and math.isfinite(value) else None

    return DiagnosticResult(
        state=state,
        target=target,
        addresses=resolution.addresses,
        dns_ms=rounded(resolution.dns_ms),
        method=method,
        samples=tuple(samples),
        sent=sent,
        received=received,
        failures=failures,
        packet_loss_percent=rounded(packet_loss),
        failure_percent=rounded(failure_percent),
        minimum_ms=rounded(min(values)) if values else None,
        average_ms=rounded(statistics.fmean(values)) if values else None,
        maximum_ms=rounded(max(values)) if values else None,
        jitter_ms=rounded(statistics.pstdev(values)) if len(values) > 1 else (0.0 if values else None),
        detail=detail,
    )


def _automatic_transport(
    address: ResolvedAddress,
    platform_name: str,
    tcp_port: int,
) -> ProbeTransport:
    if address.family == socket.AF_INET and platform_name.startswith("win"):
        return WindowsIcmpTransport()
    if address.family == socket.AF_INET and platform_name.startswith("linux"):
        return LinuxIcmpTransport()
    return TcpConnectTransport(address.family, tcp_port)


def run_diagnostics(
    target: str,
    *,
    count: int = 5,
    timeout: float = 1.0,
    interval: float = 0.2,
    tcp_port: int = 443,
    transport: ProbeTransport | None = None,
    resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    cancel_event: threading.Event | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    platform_name: str | None = None,
) -> DiagnosticResult:
    """Run one bounded diagnostic series.  Intended for a worker thread."""

    try:
        normalized_count = max(1, min(int(count), 20))
        normalized_timeout = max(0.1, min(float(timeout), 10.0))
        normalized_interval = max(0.0, min(float(interval), 5.0))
        if not 1 <= int(tcp_port) <= 65_535:
            raise ValueError("TCP port must be between 1 and 65535.")
        normalized_target = normalize_target(target)
    except (TypeError, ValueError, OverflowError) as exc:
        return DiagnosticResult(
            DiagnosticState.INVALID_TARGET,
            str(target),
            detail=str(exc),
        )

    if cancel_event is not None and cancel_event.is_set():
        return DiagnosticResult(
            DiagnosticState.CANCELLED,
            normalized_target,
            detail="Diagnostic was cancelled before DNS lookup.",
        )
    try:
        resolution = resolve_target(normalized_target, resolver=resolver)
    except (socket.gaierror, OSError, ValueError) as exc:
        return DiagnosticResult(
            DiagnosticState.DNS_FAILED,
            normalized_target,
            detail=f"DNS lookup failed: {exc}",
        )

    selected = next(
        (item for item in resolution.addresses if item.family == socket.AF_INET),
        resolution.addresses[0],
    )
    selected_transport = transport or _automatic_transport(
        selected, platform_name or sys.platform, int(tcp_port)
    )
    samples: list[ProbeSample] = []
    fallback_detail = ""

    for sequence in range(1, normalized_count + 1):
        if cancel_event is not None and cancel_event.is_set():
            return summarize_samples(
                normalized_target,
                resolution,
                samples,
                state=DiagnosticState.CANCELLED,
                detail="Diagnostic was cancelled.",
            )
        try:
            latency = selected_transport.probe(
                selected.address, normalized_timeout, sequence
            )
            samples.append(
                ProbeSample(sequence, selected_transport.method, selected.address, latency)
            )
        except ProbeUnavailable as exc:
            if transport is not None or selected_transport.method is ProbeMethod.TCP:
                return summarize_samples(
                    normalized_target,
                    resolution,
                    samples,
                    state=DiagnosticState.UNAVAILABLE,
                    detail=str(exc),
                )
            selected_transport = TcpConnectTransport(selected.family, int(tcp_port))
            fallback_detail = f"ICMP unavailable ({exc}); using TCP connection timing."
            try:
                latency = selected_transport.probe(
                    selected.address, normalized_timeout, sequence
                )
                samples.append(
                    ProbeSample(sequence, ProbeMethod.TCP, selected.address, latency)
                )
            except (TimeoutError, OSError) as fallback_exc:
                samples.append(
                    ProbeSample(
                        sequence,
                        ProbeMethod.TCP,
                        selected.address,
                        None,
                        str(fallback_exc),
                    )
                )
        except (TimeoutError, OSError) as exc:
            samples.append(
                ProbeSample(
                    sequence,
                    selected_transport.method,
                    selected.address,
                    None,
                    str(exc),
                )
            )
        if sequence < normalized_count and normalized_interval:
            sleeper(normalized_interval)

    return summarize_samples(
        normalized_target, resolution, samples, detail=fallback_detail
    )


__all__ = [
    "DiagnosticResult",
    "DiagnosticState",
    "LinuxIcmpTransport",
    "ProbeMethod",
    "ProbeSample",
    "ProbeTransport",
    "ProbeUnavailable",
    "Resolution",
    "ResolvedAddress",
    "TcpConnectTransport",
    "WindowsIcmpTransport",
    "internet_checksum",
    "normalize_target",
    "resolve_target",
    "run_diagnostics",
    "summarize_samples",
]
