import unittest

try:
    from hardware_monitor.tray import (
        TrayCapability, TrayCommand, TrayController,
    )
except ImportError:
    from v4_tray import TrayCapability, TrayCommand, TrayController


class TrayControllerTests(unittest.TestCase):
    def test_missing_optional_packages_are_a_capability_state(self):
        def missing(_name):
            raise ModuleNotFoundError

        controller = TrayController(importer=missing)
        status = controller.start()
        self.assertEqual(status.capability, TrayCapability.MISSING)
        self.assertIn("pystray", status.detail)
        self.assertEqual(controller.drain_commands(), ())

    def test_commands_are_drained_in_order_and_bounded(self):
        controller = TrayController()
        controller.commands.put_nowait(TrayCommand.SHOW)
        controller.commands.put_nowait(TrayCommand.TOGGLE_HUD)
        controller.commands.put_nowait(TrayCommand.EXIT)
        self.assertEqual(
            controller.drain_commands(limit=2),
            (TrayCommand.SHOW, TrayCommand.TOGGLE_HUD),
        )
        self.assertEqual(controller.drain_commands(), (TrayCommand.EXIT,))
        self.assertEqual(controller.stop().capability, TrayCapability.STOPPED)


if __name__ == "__main__":
    unittest.main()
