# SPDX-FileCopyrightText: 2026 FTE3600 Linux contributors
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Unit tests for fte3600-power-guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "fte3600-power-guard.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("fte3600_power_guard", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


PG = _load_module()


SAMPLE_PROC_INPUT = """\
I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name="Power Button"
P: Phys=PNP0C0C/button/input0
S: Sysfs=/devices/platform/PNP0C0C:00/input/input1
U: Uniq=
H: Handlers=kbd event1
B: PROP=0
B: EV=3
B: KEY=8000 10000000000000 0

I: Bus=0019 Vendor=0000 Product=0005 Version=0000
N: Name="Lid Switch"
P: Phys=PNP0C0D/button/input0
S: Sysfs=/devices/platform/PNP0C0D:00/input/input2
U: Uniq=
H: Handlers=event2
B: PROP=0
B: EV=21
B: SW=1

I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name="Power Button"
P: Phys=LNXPWRBN/button/input0
S: Sysfs=/devices/platform/LNXPWRBN:00/input/input3
U: Uniq=
H: Handlers=kbd event3
B: PROP=0
B: EV=3
B: KEY=8000 10000000000000 0

I: Bus=0011 Vendor=0001 Product=0001 Version=ab41
N: Name="AT Translated Set 2 keyboard"
P: Phys=isa0060/serio0/input0
S: Sysfs=/devices/platform/i8042/serio0/input/input0
U: Uniq=
H: Handlers=sysrq kbd leds event0
B: PROP=0
B: EV=120013
B: KEY=402000000 3803078f800d001 feffffdfffefffff fffffffffffffffe
"""


class TestPowerButtonDiscovery(unittest.TestCase):
    def test_discovers_both_acpi_power_buttons_and_ignores_keyboard_and_lid(self) -> None:
        devices = PG.parse_power_button_devices(SAMPLE_PROC_INPUT)
        self.assertEqual([d.event_path for d in devices], ["/dev/input/event1", "/dev/input/event3"])
        self.assertTrue(all(d.has_key_power for d in devices))


class TestPowerGuardStateMachine(unittest.TestCase):
    def test_idle_is_ungrabbed_and_lock_engages_with_grace_release(self) -> None:
        grabs: list[bool] = []
        actions: list[str] = []
        sm = PG.PowerGuardStateMachine(
            grace_sec=2.0,
            long_press_sec=1.8,
            on_grab_change=grabs.append,
            on_long_press=actions.append,
        )
        self.assertFalse(sm.guarded)
        sm.set_session_locked("/org/freedesktop/login1/session/_31", True, now=10.0)
        self.assertTrue(sm.guarded)
        self.assertEqual(grabs, [True])

        # Short press during lock screen is suppressed without triggering action.
        sm.handle_power_key(1, now=11.0)
        sm.handle_power_key(0, now=11.3)
        self.assertEqual(actions, [])

        # Unlock keeps grab for 2.0s grace window.
        sm.set_session_locked("/org/freedesktop/login1/session/_31", False, now=12.0)
        self.assertTrue(sm.guarded)
        sm.tick(now=13.5)
        self.assertTrue(sm.guarded)
        sm.tick(now=14.0)
        self.assertFalse(sm.guarded)
        self.assertEqual(grabs, [True, False])

    def test_medium_long_press_triggers_graceful_action_once(self) -> None:
        actions: list[str] = []
        sm = PG.PowerGuardStateMachine(
            grace_sec=2.0,
            long_press_sec=1.8,
            action="poweroff",
            on_long_press=actions.append,
        )
        sm.set_session_locked("/s/1", True, now=1.0)
        sm.handle_power_key(1, now=2.0)
        sm.tick(now=3.5)
        self.assertEqual(actions, [])
        sm.tick(now=3.8)
        self.assertEqual(actions, ["poweroff"])
        # Continued hold or release does not double-fire.
        sm.tick(now=4.2)
        sm.handle_power_key(0, now=4.3)
        self.assertEqual(actions, ["poweroff"])

    def test_grab_is_not_released_mid_press(self) -> None:
        grabs: list[bool] = []
        sm = PG.PowerGuardStateMachine(
            grace_sec=1.0,
            long_press_sec=1.8,
            on_grab_change=grabs.append,
        )
        sm.note_fprintd_activity(now=10.0, duration_sec=2.0)
        self.assertTrue(sm.guarded)
        sm.handle_power_key(1, now=11.5)
        # Activity expires at 12.0 while key is still held -> must stay grabbed.
        sm.tick(now=12.5)
        self.assertTrue(sm.guarded)
        # Releasing the key now releases the grab cleanly.
        sm.handle_power_key(0, now=12.6)
        self.assertFalse(sm.guarded)
        self.assertEqual(grabs, [True, False])

    def test_fprintd_finished_starts_grace_window(self) -> None:
        sm = PG.PowerGuardStateMachine(grace_sec=2.0, fprintd_activity_sec=10.0)
        sm.note_fprintd_activity(now=50.0)
        self.assertTrue(sm.guarded)
        sm.note_fprintd_finished(now=51.0)
        self.assertTrue(sm.guarded)
        sm.tick(now=52.9)
        self.assertTrue(sm.guarded)
        sm.tick(now=53.0)
        self.assertFalse(sm.guarded)


if __name__ == "__main__":
    unittest.main()
