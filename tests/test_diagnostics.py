from __future__ import annotations

import socket
import struct
import threading
import unittest
from unittest.mock import patch

try:
    from hardware_monitor.diagnostics import (
        DiagnosticState, LinuxIcmpTransport, ProbeMethod, ProbeSample,
        ProbeUnavailable, Resolution, ResolvedAddress, TcpConnectTransport,
        internet_checksum, normalize_target, resolve_target, run_diagnostics,
        summarize_samples,
    )
except ImportError:
    from v4_diagnostics import (
        DiagnosticState, LinuxIcmpTransport, ProbeMethod, ProbeSample,
        ProbeUnavailable, Resolution, ResolvedAddress, TcpConnectTransport,
        internet_checksum, normalize_target, resolve_target, run_diagnostics,
        summarize_samples,
    )


def resolver(_host, _port, **_kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 0)),
    ]


class DiagnosticsTests(unittest.TestCase):
    def test_target_validation_supports_ip_and_idna_not_commands(self) -> None:
        self.assertEqual(normalize_target(" [2001:db8::1] "), "2001:db8::1")
        self.assertEqual(normalize_target("Täst.example"), "xn--tst-qla.example")
        for invalid in ("", "--help", "https://example.com", "bad host"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_target(invalid)

    def test_resolution_deduplicates_and_measures_dns(self) -> None:
        ticks = iter((1.0, 1.025))
        resolution = resolve_target("example.com", resolver=resolver, clock=lambda: next(ticks))
        self.assertEqual(resolution.addresses, (ResolvedAddress(socket.AF_INET, "192.0.2.10"),))
        self.assertAlmostEqual(resolution.dns_ms, 25.0)

    def test_checksum_validates_a_packet(self) -> None:
        header = struct.pack("!BBHHH", 8, 0, 0, 7, 1)
        payload = b"nexus"
        checksum = internet_checksum(header + payload)
        packet = struct.pack("!BBHHH", 8, 0, checksum, 7, 1) + payload
        self.assertEqual(internet_checksum(packet), 0)

    def test_summary_does_not_call_tcp_failures_packet_loss(self) -> None:
        resolution = Resolution("test", (ResolvedAddress(socket.AF_INET, "192.0.2.1"),), 2.0)
        icmp = summarize_samples(
            "test",
            resolution,
            (
                ProbeSample(1, ProbeMethod.ICMP, "192.0.2.1", 10.0),
                ProbeSample(2, ProbeMethod.ICMP, "192.0.2.1", None, "timeout"),
            ),
        )
        self.assertEqual(icmp.packet_loss_percent, 50.0)
        tcp = summarize_samples(
            "test",
            resolution,
            (
                ProbeSample(1, ProbeMethod.TCP, "192.0.2.1", 5.0),
                ProbeSample(2, ProbeMethod.TCP, "192.0.2.1", None, "refused"),
            ),
        )
        self.assertIsNone(tcp.packet_loss_percent)
        self.assertEqual(tcp.failure_percent, 50.0)

    def test_user_triggered_series_is_bounded_and_deterministic(self) -> None:
        class Transport:
            method = ProbeMethod.ICMP

            def probe(self, _address, _timeout, sequence):
                if sequence == 2:
                    raise TimeoutError("timeout")
                return sequence * 10.0

        sleeps = []
        result = run_diagnostics(
            "example.com",
            count=3,
            interval=0.25,
            resolver=resolver,
            transport=Transport(),
            sleeper=sleeps.append,
        )
        self.assertEqual(result.state, DiagnosticState.PARTIAL)
        self.assertEqual((result.sent, result.received), (3, 2))
        self.assertEqual(result.average_ms, 20.0)
        self.assertEqual(sleeps, [0.25, 0.25])

    def test_icmp_capability_failure_uses_honest_tcp_fallback(self) -> None:
        class NoIcmp:
            method = ProbeMethod.ICMP

            def probe(self, *_args):
                raise ProbeUnavailable("not permitted")

        class Tcp:
            method = ProbeMethod.TCP

            def probe(self, _address, _timeout, sequence):
                return float(sequence)

        module = run_diagnostics.__module__
        with patch(f"{module}._automatic_transport", return_value=NoIcmp()), patch(
            f"{module}.TcpConnectTransport", return_value=Tcp()
        ):
            result = run_diagnostics("example.com", count=2, interval=0, resolver=resolver)
        self.assertEqual(result.method, ProbeMethod.TCP)
        self.assertIsNone(result.packet_loss_percent)
        self.assertIn("ICMP unavailable", result.detail)

    def test_cancellation_dns_failure_and_invalid_target_are_states(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        self.assertEqual(
            run_diagnostics("example.com", cancel_event=cancelled).state,
            DiagnosticState.CANCELLED,
        )
        failed = run_diagnostics(
            "example.com",
            resolver=lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror("no")),
        )
        self.assertEqual(failed.state, DiagnosticState.DNS_FAILED)
        self.assertEqual(run_diagnostics("--help").state, DiagnosticState.INVALID_TARGET)

    def test_linux_permission_error_is_capability_failure(self) -> None:
        transport = LinuxIcmpTransport(
            socket_factory=lambda *_args: (_ for _ in ()).throw(PermissionError("denied"))
        )
        with self.assertRaises(ProbeUnavailable):
            transport.probe("192.0.2.1", 1, 1)

    def test_tcp_transport_closes_socket(self) -> None:
        class FakeSocket:
            closed = False
            timeout = None
            destination = None

            def settimeout(self, value):
                self.timeout = value

            def connect(self, destination):
                self.destination = destination

            def close(self):
                self.closed = True

        fake = FakeSocket()
        ticks = iter((1.0, 1.007))
        transport = TcpConnectTransport(
            socket.AF_INET,
            443,
            socket_factory=lambda *_args: fake,
            clock=lambda: next(ticks),
        )
        self.assertAlmostEqual(transport.probe("192.0.2.1", 0.5, 1), 7.0)
        self.assertTrue(fake.closed)
        self.assertEqual(fake.destination, ("192.0.2.1", 443))


if __name__ == "__main__":
    unittest.main()
