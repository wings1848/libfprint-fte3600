# SPDX-License-Identifier: LGPL-2.1-or-later
"""Smoke tests for the GTK4 + libadwaita GUI layer."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core_unit import FakeBackend  # noqa: E402


def _has_gtk_adw() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401

        return True
    except Exception:
        return False


@unittest.skipUnless(_has_gtk_adw(), "GTK4 + libadwaita PyGObject bindings not installed")
class GuiSmokeTests(unittest.TestCase):
    def test_gui_modules_import_and_progress_ring_state(self) -> None:
        from fte3600_toolkit.gui.app import APP_ID, ToolkitApplication
        from fte3600_toolkit.gui.ring import ProgressRing

        self.assertEqual(APP_ID, "org.freedesktop.libfprint.Fte3600Toolkit")
        self.assertIsNotNone(ToolkitApplication)

        from gi.repository import Adw, Gdk, Gio, Gtk

        # Instantiate GTK widgets only when a display server is actually reachable.
        if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
            self.skipTest("No Wayland/X11 display environment variable set")
        init_res = Gtk.init_check()
        init_ok = init_res[0] if isinstance(init_res, tuple) else bool(init_res)
        if not init_ok or Gdk.Display.get_default() is None:
            self.skipTest("Wayland/X11 display is not reachable")

        Adw.init()
        ring = ProgressRing(total_stages=9)
        self.assertEqual(ring.total_stages, 9)
        ring.set_progress(4, total=9, state="ready")
        self.assertEqual(ring.completed_stages, 4)

        app = ToolkitApplication(
            backend_factory=FakeBackend,
            lang="en",
            sound=False,
            notify=False,
            inhibit_power_key=False,
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        # Register the GApplication so GApplication::startup runs before adding a window.
        app.register(None)
        from fte3600_toolkit.gui.window import ToolkitWindow

        win = ToolkitWindow(
            app,
            backend_factory=FakeBackend,
            lang="en",
            sound=False,
            notify=False,
            inhibit_power_key=False,
        )
        self.assertIsNotNone(win._device_info)
        self.assertEqual(win._device_info.num_enroll_stages, 9)
        self.assertEqual(win._ring.total_stages, 9)
        win.destroy()


if __name__ == "__main__":
    unittest.main()
