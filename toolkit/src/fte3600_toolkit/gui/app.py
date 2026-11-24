# SPDX-License-Identifier: LGPL-2.1-or-later
"""Single-instance GTK4 + libadwaita application entrypoint."""

from __future__ import annotations

import argparse
import sys
from typing import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

from .. import __version__  # noqa: E402
from ..core import FprintBackend, resolve_language  # noqa: E402
from .window import ToolkitWindow  # noqa: E402

APP_ID = "org.freedesktop.libfprint.Fte3600Toolkit"


class ToolkitApplication(Adw.Application):
    """Single-instance desktop application focusing the existing window on relaunch."""

    def __init__(
        self,
        *,
        backend_factory: Callable[[], FprintBackend] | None = None,
        state_file: str | None = None,
        lang: str = "en",
        sound: bool = True,
        notify: bool = True,
        inhibit_power_key: bool = True,
        flags: Gio.ApplicationFlags = Gio.ApplicationFlags.DEFAULT_FLAGS,
    ) -> None:
        super().__init__(application_id=APP_ID, flags=flags)
        self._backend_factory = backend_factory
        self._state_file = state_file
        self._lang = lang
        self._sound = sound
        self._notify = notify
        self._inhibit_power_key = inhibit_power_key
        self._window: ToolkitWindow | None = None

    def do_activate(self) -> None:
        if self._window is None:
            self._window = ToolkitWindow(
                self,
                backend_factory=self._backend_factory,
                state_file=self._state_file,
                lang=self._lang,
                sound=self._sound,
                notify=self._notify,
                inhibit_power_key=self._inhibit_power_key,
            )
        self._window.present()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fte3600-toolkit-gui")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--lang", choices=("auto", "en", "zh"), default="auto")
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--no-sound", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--no-power-inhibit", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    app = ToolkitApplication(
        state_file=args.state_file,
        lang=resolve_language(args.lang),
        sound=not args.no_sound,
        notify=not args.no_notify,
        inhibit_power_key=not args.no_power_inhibit,
    )
    return int(app.run([]))


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
