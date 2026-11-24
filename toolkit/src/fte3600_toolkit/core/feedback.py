# SPDX-License-Identifier: LGPL-2.1-or-later
"""Non-blocking audio and desktop notification feedback for headless sensors."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

SOUND_FILES: dict[str, str] = {
    "ready": "/usr/share/sounds/freedesktop/stereo/message.oga",
    "stage": "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga",
    "ok": "/usr/share/sounds/freedesktop/stereo/complete.oga",
    "retry": "/usr/share/sounds/freedesktop/stereo/bell.oga",
    "fail": "/usr/share/sounds/freedesktop/stereo/dialog-error.oga",
}

CANBERRA_IDS: dict[str, str] = {
    "ready": "message",
    "stage": "message-new-instant",
    "ok": "complete",
    "retry": "bell",
    "fail": "dialog-error",
}


class FeedbackChannel:
    """Best-effort audio + desktop notification dispatcher.

    Never blocks the caller and never raises if desktop daemons are absent.
    """

    def __init__(
        self,
        *,
        sound: bool = True,
        notify: bool = True,
        app_name: str = "FTE3600 Fingerprint",
        terminal_bell: bool = False,
    ) -> None:
        self.sound_enabled = sound
        self.notify_enabled = notify
        self.app_name = app_name
        self.terminal_bell = terminal_bell
        self._paplay = shutil.which("paplay")
        self._canberra = shutil.which("canberra-gtk-play")
        self._notify_send = shutil.which("notify-send")

    def _spawn(self, cmd: list[str]) -> None:
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass

    def play_sound(self, kind: str) -> None:
        if not self.sound_enabled:
            return
        path = SOUND_FILES.get(kind)
        if path and os.path.exists(path) and self._paplay:
            self._spawn([self._paplay, path])
            return
        if self._canberra and kind in CANBERRA_IDS:
            self._spawn([self._canberra, "-i", CANBERRA_IDS[kind]])
            return
        if self.terminal_bell:
            try:
                sys.stderr.write("\a")
                sys.stderr.flush()
            except OSError:
                pass

    def send_notification(self, title: str, body: str, kind: str = "ready") -> None:
        if not self.notify_enabled:
            return
        if not self._notify_send:
            return
        # Skip desktop notification when no session bus/display is attached (e.g. pure SSH).
        if not (
            os.environ.get("DBUS_SESSION_BUS_ADDRESS")
            or os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("DISPLAY")
        ):
            return
        urgency = "critical" if kind == "fail" else "normal"
        self._spawn(
            [
                self._notify_send,
                "-a",
                self.app_name,
                "-i",
                "fingerprint",
                "-u",
                urgency,
                "-t",
                "4000",
                title,
                body,
            ]
        )

    def emit(self, kind: str, title: str, body: str) -> None:
        self.play_sound(kind)
        self.send_notification(title, body, kind)
