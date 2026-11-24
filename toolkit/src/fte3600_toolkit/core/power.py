# SPDX-License-Identifier: LGPL-2.1-or-later
"""Unprivileged power-key inhibition and KDE PowerDevil read-only diagnostics.

On GPD handhelds/mini-laptops (such as GPD Pocket 3) the FTE3600 fingerprint pad
shares the physical power button. This module:
  1. Requests an unprivileged systemd-logind `handle-power-key` inhibitor lock
     during active enrollment/testing (gracefully degrading when unavailable).
  2. Reads KDE PowerDevil config (read-only) to warn if short presses may open
     the shutdown menu. It never modifies system or user settings.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PowerDevilStatus:
    """Read-only inspection of KDE PowerDevil's PowerButtonAction."""

    config_present: bool
    profiles: dict[str, int | None]

    @property
    def may_trigger_power_menu(self) -> bool:
        if not self.config_present:
            return False
        # PowerButtonAction=0 means "Do nothing". Missing or non-zero may act on KEY_POWER.
        return any(val is None or val != 0 for val in self.profiles.values())


def inspect_powerdevil_config(config_path: Path | None = None) -> PowerDevilStatus:
    """Parse ~/.config/powerdevilrc without mutating it."""
    if config_path is None:
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg_cfg) if xdg_cfg else Path.home() / ".config"
        config_path = base / "powerdevilrc"

    if not config_path.exists():
        return PowerDevilStatus(config_present=False, profiles={})

    profiles: dict[str, int | None] = {
        "AC": None,
        "Battery": None,
        "LowBattery": None,
    }
    current_profile: str | None = None
    in_suspend_section = False

    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return PowerDevilStatus(config_present=False, profiles={})

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            # KDE nested groups look like [AC][SuspendAndShutdown]
            parts = [part.strip("[]") for part in line.split("][")]
            if len(parts) == 2 and parts[0] in profiles and parts[1] == "SuspendAndShutdown":
                current_profile = parts[0]
                in_suspend_section = True
            else:
                current_profile = None
                in_suspend_section = False
            continue
        if in_suspend_section and current_profile is not None and "=" in line:
            key, _, raw_val = line.partition("=")
            if key.strip() == "PowerButtonAction":
                try:
                    profiles[current_profile] = int(raw_val.strip())
                except ValueError:
                    profiles[current_profile] = None

    return PowerDevilStatus(config_present=True, profiles=profiles)


class PowerKeyInhibitor:
    """Context manager holding a systemd-logind handle-power-key inhibitor lock.

    Uses Gio.DBus on the system bus without requiring root privileges.
    If logind is unreachable (containers, mock D-Bus tests, non-systemd hosts),
    enters cleanly with `active == False`.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        who: str = "fte3600-toolkit",
        why: str = "Fingerprint sensor shares the power button during enrollment/testing",
    ) -> None:
        self._enabled = enabled
        self._who = who
        self._why = why
        self._fd: int | None = None
        self.error: str | None = None

    @property
    def active(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        if not self._enabled or self._fd is not None:
            return self.active
        # Never touch the real host logind when running under isolated test bus.
        if os.environ.get("FTE3600_TOOLKIT_DISABLE_LOGIND") == "1":
            return False
        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib

            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            result, fd_list = bus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "Inhibit",
                GLib.Variant(
                    "(ssss)",
                    ("handle-power-key", self._who, self._why, "block"),
                ),
                GLib.VariantType("(h)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
                None,
            )
            if fd_list is not None and result is not None:
                fd_index = result.unpack()[0]
                self._fd = fd_list.steal_fds()[fd_index]
                return True
        except Exception as exc:  # pragma: no cover - host logind dependent
            self.error = str(exc)
        return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "PowerKeyInhibitor":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()
