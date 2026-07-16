import unittest

from hardware_monitor.monitor import NetworkInterfaceInfo
from hardware_monitor.network import (
    NetworkRateTracker,
    format_bytes,
    format_link_speed,
    format_rate,
)


def interface(received, sent, luid=1):
    return NetworkInterfaceInfo(
        luid=luid,
        index=7,
        alias="Ethernet",
        description="Test adapter",
        kind="Ethernet",
        receive_link_bps=1_000_000_000,
        transmit_link_bps=1_000_000_000,
        received_bytes=received,
        sent_bytes=sent,
    )


class NetworkTrackerTests(unittest.TestCase):
    def test_first_sample_establishes_baseline(self):
        tracker = NetworkRateTracker()
        rates = tracker.update((interface(1000, 500),), 10.0)
        self.assertEqual(rates.download_bps, 0)
        self.assertEqual(rates.upload_bps, 0)

    def test_known_delta_and_session_totals(self):
        tracker = NetworkRateTracker()
        tracker.update((interface(1000, 500),), 10.0)
        rates = tracker.update((interface(1_049_576, 524_788),), 12.0)
        self.assertEqual(rates.download_bps, 524_288)
        self.assertEqual(rates.upload_bps, 262_144)
        self.assertEqual(rates.session_received_bytes, 1_048_576)
        self.assertEqual(rates.session_sent_bytes, 524_288)

    def test_counter_rollback_resets_adapter_baseline(self):
        tracker = NetworkRateTracker()
        tracker.update((interface(5000, 4000),), 1.0)
        rates = tracker.update((interface(10, 20),), 2.0)
        self.assertEqual(rates.download_bps, 0)
        self.assertEqual(rates.upload_bps, 0)

    def test_interface_add_remove_does_not_create_spike(self):
        tracker = NetworkRateTracker()
        tracker.update((interface(100, 100, 1),), 1.0)
        rates = tracker.update((interface(500, 500, 2),), 2.0)
        self.assertEqual(rates.download_bps, 0)
        self.assertEqual(len(rates.adapters), 1)

    def test_reset_starts_a_fresh_baseline(self):
        tracker = NetworkRateTracker()
        tracker.update((interface(100, 100),), 1.0)
        tracker.update((interface(200, 200),), 2.0)
        tracker.reset_session()
        rates = tracker.update((interface(300, 350),), 3.0)
        self.assertEqual(rates.download_bps, 0)
        self.assertEqual(rates.upload_bps, 0)
        self.assertEqual(rates.session_received_bytes, 0)
        self.assertEqual(rates.session_sent_bytes, 0)
        rates = tracker.update((interface(400, 500),), 4.0)
        self.assertEqual(rates.download_bps, 100)
        self.assertEqual(rates.session_received_bytes, 100)
        self.assertEqual(rates.session_sent_bytes, 150)

    def test_snapshot_started_before_reset_cannot_become_new_baseline(self):
        tracker = NetworkRateTracker()
        tracker.update((interface(100, 100),), 10.0)
        tracker.reset_session(12.0)
        stale = tracker.update((interface(300, 350),), 11.0)
        self.assertEqual(stale.session_received_bytes, 0)
        tracker.update((interface(400, 500),), 13.0)
        rates = tracker.update((interface(500, 650),), 14.0)
        self.assertEqual(rates.session_received_bytes, 100)
        self.assertEqual(rates.session_sent_bytes, 150)

    def test_formatters(self):
        self.assertEqual(format_bytes(1024), "1.0 KiB")
        self.assertEqual(format_rate(1024 * 1024), "1.0 MiB/s")
        self.assertEqual(format_link_speed(2_500_000_000), "2.5 Gbps")


if __name__ == "__main__":
    unittest.main()
