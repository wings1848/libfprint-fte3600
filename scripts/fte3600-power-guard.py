#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 FTE3600 Linux contributors
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Power-button coexistence guard for combo power-button fingerprint sensors.

On laptops and handhelds where the FTE3600 sensor is integrated into the
physical power button (such as the GPD Pocket 3), pressing the button to scan
a finger also emits Linux KEY_POWER events.

This daemon keeps normal short-press power-button behavior intact whenever
fingerprint authentication is idle, and temporarily grabs the ACPI power
button input device(s) (EVIOCGRAB) only while:
  1. A graphical login1 session is locked (LockedHint == True), OR
  2. fprintd is actively running a verification/enrollment session, OR
  3. Within a short release grace window (default 2.0 s) after the above ends.

While guarded:
  - Short presses (< long_press_sec, default 1.8 s) are suppressed so finger
    scans never trigger accidental suspend or shutdown.
  - Holding the button for >= long_press_sec triggers a graceful systemd-logind
    action (PowerOff / Suspend / Hibernate, default PowerOff) well before the
    motherboard EC 5-second hard cut-off.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import logging
import os
from pathlib import Path
import re
import signal
import struct
import sys
import time
from typing import Callable, Iterable, Sequence

LOG = logging.getLogger("fte3600-power-guard")

# Linux input constants (stable kernel ABI across 64-bit / 32-bit).
EV_KEY = 0x01
KEY_POWER = 116
EVIOCGRAB = 0x40044590  # _IOW('E', 0x90, int)

# struct input_event on Linux: { struct timeval time; __u16 type; __u16 code; __s32 value; }
INPUT_EVENT_FORMAT = "llHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FORMAT)

DEFAULT_GRACE_SEC = 2.0
DEFAULT_LONG_PRESS_SEC = 1.8
DEFAULT_FPRINTD_ACTIVITY_SEC = 12.0
DEFAULT_ACTION = "poweroff"

VALID_ACTIONS = {
    "poweroff": "PowerOff",
    "suspend": "Suspend",
    "hibernate": "Hibernate",
    "ignore": None,
}

_POWER_NAME_RE = re.compile(r"power\s+button", re.IGNORECASE)
_POWER_PHYS_RE = re.compile(r"(PNP0C0C|LNXPWRBN)", re.IGNORECASE)
_EVENT_HANDLER_RE = re.compile(r"\bevent(\d+)\b")


@dataclass(frozen=True)
class InputDeviceInfo:
    """Parsed summary of one /proc/bus/input/devices stanza."""

    name: str
    phys: str
    event_path: str
    has_key_power: bool


def _key_bitmask_has_bit(hex_words: str, bit: int) -> bool:
    """Check whether a /proc/bus/input/devices B: KEY=... line has `bit` set."""
    words = [int(part, 16) for part in hex_words.strip().split() if part]
    if not words:
        return False
    word_bits = struct.calcsize("L") * 8
    word_index = bit // word_bits
    bit_offset = bit % word_bits
    # Kernel prints the most-significant unsigned long first.
    if word_index >= len(words):
        return False
    word = words[len(words) - 1 - word_index]
    return bool(word & (1 << bit_offset))


def parse_power_button_devices(proc_text: str) -> list[InputDeviceInfo]:
    """Return all ACPI power-button evdev nodes from /proc/bus/input/devices."""
    results: list[InputDeviceInfo] = []
    for block in proc_text.strip().split("\n\n"):
        name = ""
        phys = ""
        handlers = ""
        key_bits = ""
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if line.startswith("N: Name="):
                name = line[len("N: Name="):].strip().strip('"')
            elif line.startswith("P: Phys="):
                phys = line[len("P: Phys="):].strip()
            elif line.startswith("H: Handlers="):
                handlers = line[len("H: Handlers="):].strip()
            elif line.startswith("B: KEY="):
                key_bits = line[len("B: KEY="):].strip()
        if not handlers:
            continue
        match = _EVENT_HANDLER_RE.search(handlers)
        if not match:
            continue
        has_power = _key_bitmask_has_bit(key_bits, KEY_POWER)
        if not has_power:
            continue
        if _POWER_NAME_RE.search(name) or _POWER_PHYS_RE.search(phys):
            results.append(
                InputDeviceInfo(
                    name=name,
                    phys=phys,
                    event_path=f"/dev/input/event{match.group(1)}",
                    has_key_power=True,
                )
            )
    return results


def discover_power_button_paths(
    proc_path: str = "/proc/bus/input/devices",
) -> list[str]:
    """Discover all ACPI power-button /dev/input/eventN paths on this host."""
    text = Path(proc_path).read_text(encoding="utf-8", errors="replace")
    return [dev.event_path for dev in parse_power_button_devices(text)]


class PowerGuardStateMachine:
    """Pure, deterministic state machine for power-button guarding.

    Separated from D-Bus and evdev I/O so every timing edge case can be
    exhaustively unit-tested without root privileges or hardware.
    """

    def __init__(
        self,
        *,
        grace_sec: float = DEFAULT_GRACE_SEC,
        long_press_sec: float = DEFAULT_LONG_PRESS_SEC,
        fprintd_activity_sec: float = DEFAULT_FPRINTD_ACTIVITY_SEC,
        action: str = DEFAULT_ACTION,
        on_grab_change: Callable[[bool], None] | None = None,
        on_long_press: Callable[[str], None] | None = None,
    ) -> None:
        if grace_sec < 0:
            raise ValueError("grace_sec must be >= 0")
        if long_press_sec <= 0:
            raise ValueError("long_press_sec must be > 0")
        if fprintd_activity_sec <= 0:
            raise ValueError("fprintd_activity_sec must be > 0")
        if action not in VALID_ACTIONS:
            raise ValueError(f"Unsupported action: {action!r}")

        self.grace_sec = grace_sec
        self.long_press_sec = long_press_sec
        self.fprintd_activity_sec = fprintd_activity_sec
        self.action = action
        self._on_grab_change = on_grab_change
        self._on_long_press = on_long_press

        self._locked_sessions: set[str] = set()
        self._fprintd_active_until: float = 0.0
        self._grace_until: float = 0.0
        self._guarded: bool = False

        self._key_down_at: float | None = None
        self._long_press_fired: bool = False

    @property
    def guarded(self) -> bool:
        return self._guarded

    @property
    def key_down_at(self) -> float | None:
        return self._key_down_at

    def should_guard(self, now: float) -> bool:
        if self._locked_sessions:
            return True
        if now < self._fprintd_active_until:
            return True
        if now < self._grace_until:
            return True
        if self._key_down_at is not None:
            # Never release grab mid-press, otherwise key-up could leak.
            return True
        return False

    def next_deadline(self, now: float) -> float | None:
        """Return earliest monotonic timestamp when a timer check is needed."""
        deadlines: list[float] = []
        if self._key_down_at is not None and not self._long_press_fired:
            deadlines.append(self._key_down_at + self.long_press_sec)
        if not self._locked_sessions:
            if self._fprintd_active_until > now:
                deadlines.append(self._fprintd_active_until)
            if self._grace_until > now:
                deadlines.append(self._grace_until)
        return min(deadlines) if deadlines else None

    def _sync_guard(self, now: float) -> None:
        want = self.should_guard(now)
        if want != self._guarded:
            self._guarded = want
            if self._on_grab_change is not None:
                self._on_grab_change(want)

    def set_session_locked(self, session_path: str, locked: bool, now: float) -> None:
        was_locked = bool(self._locked_sessions)
        if locked:
            self._locked_sessions.add(session_path)
        else:
            self._locked_sessions.discard(session_path)
            if was_locked and not self._locked_sessions:
                self._grace_until = max(self._grace_until, now + self.grace_sec)
        self._sync_guard(now)

    def note_fprintd_activity(self, now: float, duration_sec: float | None = None) -> None:
        window = self.fprintd_activity_sec if duration_sec is None else duration_sec
        self._fprintd_active_until = max(self._fprintd_active_until, now + max(0.0, window))
        self._sync_guard(now)

    def note_fprintd_finished(self, now: float) -> None:
        self._fprintd_active_until = 0.0
        self._grace_until = max(self._grace_until, now + self.grace_sec)
        self._sync_guard(now)

    def handle_power_key(self, value: int, now: float) -> None:
        """Process a KEY_POWER event (value: 1=press, 0=release, 2=repeat)."""
        if not self._guarded:
            return
        if value == 1:
            if self._key_down_at is None:
                self._key_down_at = now
                self._long_press_fired = False
        elif value == 2:
            self.tick(now)
        elif value == 0:
            if self._key_down_at is not None:
                held = now - self._key_down_at
                if not self._long_press_fired and held + 1e-9 >= self.long_press_sec:
                    self._fire_long_press()
                self._key_down_at = None
                self._long_press_fired = False
            self._sync_guard(now)

    def _fire_long_press(self) -> None:
        self._long_press_fired = True
        if self.action != "ignore" and self._on_long_press is not None:
            self._on_long_press(self.action)

    def tick(self, now: float) -> None:
        """Advance timers: fire medium-long press or release grab after grace."""
        if (
            self._key_down_at is not None
            and not self._long_press_fired
            and (now - self._key_down_at) + 1e-9 >= self.long_press_sec
        ):
            self._fire_long_press()
        self._sync_guard(now)


class EvdevPowerGuard:
    """Manages non-blocking evdev file descriptors and EVIOCGRAB state."""

    def __init__(self, paths: Sequence[str]) -> None:
        self._paths = list(dict.fromkeys(paths))
        self._fds: dict[str, int] = {}
        self._grabbed: bool = False

    @property
    def fds(self) -> dict[str, int]:
        return dict(self._fds)

    def open(self) -> None:
        for path in self._paths:
            if path in self._fds:
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            self._fds[path] = fd
            LOG.info("Opened power button input device %s (fd=%d)", path, fd)

    def set_grab(self, grab: bool) -> None:
        if grab == self._grabbed:
            return
        flag = 1 if grab else 0
        for path, fd in list(self._fds.items()):
            try:
                fcntl.ioctl(fd, EVIOCGRAB, flag)
            except OSError as exc:
                LOG.warning("EVIOCGRAB(%d) failed on %s: %s", flag, path, exc)
        self._grabbed = grab
        LOG.info("Power button guard %s", "ENGAGED (EVIOCGRAB)" if grab else "RELEASED")

    def read_key_power_values(self, fd: int) -> list[int]:
        values: list[int] = []
        while True:
            try:
                chunk = os.read(fd, INPUT_EVENT_SIZE * 16)
            except BlockingIOError:
                break
            except OSError as exc:
                LOG.warning("Read error on fd=%d: %s", fd, exc)
                break
            if not chunk:
                break
            offset = 0
            while offset + INPUT_EVENT_SIZE <= len(chunk):
                _sec, _usec, ev_type, code, value = struct.unpack_from(
                    INPUT_EVENT_FORMAT, chunk, offset
                )
                offset += INPUT_EVENT_SIZE
                if ev_type == EV_KEY and code == KEY_POWER:
                    values.append(value)
        return values

    def close(self) -> None:
        if self._grabbed:
            self.set_grab(False)
        for path, fd in list(self._fds.items()):
            try:
                os.close(fd)
            except OSError:
                pass
            LOG.info("Closed power button input device %s", path)
        self._fds.clear()


def run_daemon(args: argparse.Namespace) -> int:
    from gi.repository import Gio, GLib  # type: ignore[import-not-found]

    paths = list(args.device) if args.device else discover_power_button_paths()
    if not paths:
        LOG.error("No ACPI Power Button /dev/input/event* devices found.")
        return 1

    evdev = EvdevPowerGuard(paths)
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    timer_id: list[int | None] = [None]
    io_watches: list[int] = []

    def trigger_logind_action(action: str) -> None:
        method = VALID_ACTIONS.get(action)
        if not method:
            return
        LOG.info("Medium-long power press detected; invoking login1.%s", method)
        try:
            bus.call_sync(
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                method,
                GLib.Variant("(b)", (False,)),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.error("Failed to invoke login1.%s: %s", method, exc)

    sm = PowerGuardStateMachine(
        grace_sec=args.grace_sec,
        long_press_sec=args.long_press_sec,
        fprintd_activity_sec=args.fprintd_window_sec,
        action=args.long_press_action,
        on_grab_change=evdev.set_grab,
        on_long_press=trigger_logind_action,
    )

    def reschedule_timer() -> None:
        if timer_id[0] is not None:
            GLib.source_remove(timer_id[0])
            timer_id[0] = None
        now = time.monotonic()
        deadline = sm.next_deadline(now)
        if deadline is None:
            return
        delay_ms = max(25, int((deadline - now) * 1000) + 15)

        def _on_timer() -> bool:
            timer_id[0] = None
            sm.tick(time.monotonic())
            reschedule_timer()
            return False

        timer_id[0] = GLib.timeout_add(delay_ms, _on_timer)

    def sync_initial_locked_sessions() -> None:
        now = time.monotonic()
        try:
            res = bus.call_sync(
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "ListSessions",
                None,
                GLib.VariantType("(a(susso))"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            sessions = res.unpack()[0]
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Could not enumerate initial login1 sessions: %s", exc)
            return
        for _sid, _uid, _user, _seat, obj_path in sessions:
            try:
                prop = bus.call_sync(
                    "org.freedesktop.login1",
                    obj_path,
                    "org.freedesktop.DBus.Properties",
                    "Get",
                    GLib.Variant("(ss)", ("org.freedesktop.login1.Session", "LockedHint")),
                    GLib.VariantType("(v)"),
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
                locked = bool(prop.unpack()[0])
                if locked:
                    sm.set_session_locked(obj_path, True, now)
            except Exception:  # noqa: BLE001
                continue

    def on_login1_props(
        _conn, _sender, obj_path, _iface, _signal, params, *_rest
    ) -> None:
        iface_name, changed, _invalidated = params.unpack()
        if iface_name != "org.freedesktop.login1.Session":
            return
        if "LockedHint" in changed:
            locked = bool(changed["LockedHint"])
            LOG.debug("Session %s LockedHint=%s", obj_path, locked)
            sm.set_session_locked(obj_path, locked, time.monotonic())
            reschedule_timer()

    def on_fprintd_owner(
        _conn, _sender, _obj_path, _iface, _signal, params, *_rest
    ) -> None:
        name, _old_owner, new_owner = params.unpack()
        if name != "net.reactivated.Fprint":
            return
        now = time.monotonic()
        if new_owner:
            LOG.debug("fprintd appeared on system bus (%s)", new_owner)
            sm.note_fprintd_activity(now)
        else:
            LOG.debug("fprintd exited system bus")
            sm.note_fprintd_finished(now)
        reschedule_timer()

    def on_fprintd_device_signal(
        _conn, _sender, _obj_path, _iface, signal_name, params, *_rest
    ) -> None:
        now = time.monotonic()
        if signal_name in ("VerifyFingerSelected", "EnrollStatus", "VerifyStatus"):
            done = False
            if signal_name in ("EnrollStatus", "VerifyStatus"):
                unpacked = params.unpack()
                if len(unpacked) >= 2:
                    done = bool(unpacked[1])
            LOG.debug("fprintd signal %s done=%s", signal_name, done)
            if done:
                sm.note_fprintd_finished(now)
            else:
                sm.note_fprintd_activity(now)
            reschedule_timer()

    subscriptions: list[int] = []
    loop = GLib.MainLoop()

    def _stop(*_sig_args) -> bool:
        loop.quit()
        return False

    try:
        evdev.open()
        for path, fd in evdev.fds.items():
            def _on_io(_fd: int, _cond, *, dev_fd: int = fd) -> bool:
                now = time.monotonic()
                for val in evdev.read_key_power_values(dev_fd):
                    sm.handle_power_key(val, now)
                reschedule_timer()
                return True

            io_watches.append(GLib.io_add_watch(fd, GLib.IO_IN, _on_io))

        subscriptions.append(
            bus.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                on_login1_props,
            )
        )
        subscriptions.append(
            bus.signal_subscribe(
                "org.freedesktop.DBus",
                "org.freedesktop.DBus",
                "NameOwnerChanged",
                "/org/freedesktop/DBus",
                "net.reactivated.Fprint",
                Gio.DBusSignalFlags.NONE,
                on_fprintd_owner,
            )
        )
        subscriptions.append(
            bus.signal_subscribe(
                "net.reactivated.Fprint",
                "net.reactivated.Fprint.Device",
                None,
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                on_fprintd_device_signal,
            )
        )

        sync_initial_locked_sessions()
        reschedule_timer()

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _stop)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _stop)

        LOG.info(
            "fte3600-power-guard running (devices=%s, long_press=%.1fs -> %s, grace=%.1fs)",
            ",".join(paths),
            args.long_press_sec,
            args.long_press_action,
            args.grace_sec,
        )
        loop.run()
    finally:
        if timer_id[0] is not None:
            GLib.source_remove(timer_id[0])
        for watch_id in io_watches:
            GLib.source_remove(watch_id)
        for sub_id in subscriptions:
            bus.signal_unsubscribe(sub_id)
        evdev.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fte3600-power-guard",
        description=(
            "Coexistence guard for combo power-button FTE3600 fingerprint readers: "
            "suppresses short KEY_POWER presses only during lock screen / fprintd "
            "activity while preserving normal idle power-button behavior and "
            "medium-long-press graceful shutdown."
        ),
    )
    parser.add_argument(
        "--device",
        action="append",
        help="Explicit /dev/input/eventN path (repeatable; defaults to auto-discovery).",
    )
    parser.add_argument(
        "--grace-sec",
        type=float,
        default=DEFAULT_GRACE_SEC,
        help=f"Grace window after unlock/auth before releasing grab (default: {DEFAULT_GRACE_SEC}s).",
    )
    parser.add_argument(
        "--long-press-sec",
        type=float,
        default=DEFAULT_LONG_PRESS_SEC,
        help=f"Hold duration to trigger graceful action while guarded (default: {DEFAULT_LONG_PRESS_SEC}s).",
    )
    parser.add_argument(
        "--fprintd-window-sec",
        type=float,
        default=DEFAULT_FPRINTD_ACTIVITY_SEC,
        help=f"Guard window per fprintd activity pulse (default: {DEFAULT_FPRINTD_ACTIVITY_SEC}s).",
    )
    parser.add_argument(
        "--long-press-action",
        choices=sorted(VALID_ACTIONS.keys()),
        default=DEFAULT_ACTION,
        help=f"login1 action on medium-long press while guarded (default: {DEFAULT_ACTION}).",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print auto-discovered ACPI power button devices and exit.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    if args.list_devices:
        for path in discover_power_button_paths():
            print(path)
        return 0
    return run_daemon(args)


if __name__ == "__main__":
    sys.exit(main())
