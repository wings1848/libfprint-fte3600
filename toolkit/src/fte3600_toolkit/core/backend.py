# SPDX-License-Identifier: LGPL-2.1-or-later
"""fprintd D-Bus backend abstraction and Gio.DBus implementation."""

from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Callable, Protocol

FPRINTD_BUS_NAME = "net.reactivated.Fprint"
FPRINTD_MANAGER_PATH = "/net/reactivated/Fprint/Manager"
FPRINTD_MANAGER_IFACE = "net.reactivated.Fprint.Manager"
FPRINTD_DEVICE_IFACE = "net.reactivated.Fprint.Device"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Metadata exposed by net.reactivated.Fprint.Device."""

    object_path: str
    name: str
    scan_type: str
    num_enroll_stages: int
    enrolled_fingers: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "object_path": self.object_path,
            "name": self.name,
            "scan_type": self.scan_type,
            "num_enroll_stages": self.num_enroll_stages,
            "enrolled_fingers": list(self.enrolled_fingers),
        }


class FprintError(RuntimeError):
    """Normalized error raised by any FprintBackend implementation."""

    def __init__(self, code: str, message: str, dbus_name: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.dbus_name = dbus_name


class FprintBackend(Protocol):
    """Protocol seam between the shared state machine and fprintd D-Bus."""

    def get_default_device(self, username: str = "") -> DeviceInfo: ...

    def list_enrolled_fingers(self, username: str = "") -> list[str]: ...

    def delete_enrolled_finger(self, finger: str, username: str = "") -> None: ...

    def delete_enrolled_fingers(self, username: str = "") -> None: ...

    def claim(self, username: str = "") -> None: ...

    def release(self) -> None: ...

    def verify_start(self, finger: str) -> None: ...

    def verify_stop(self) -> None: ...

    def enroll_start(self, finger: str) -> None: ...

    def enroll_stop(self) -> None: ...

    def subscribe_signals(
        self,
        on_verify_status: Callable[[str, bool], None],
        on_enroll_status: Callable[[str, bool], None],
    ) -> int: ...

    def unsubscribe_signals(self, subscription_id: int) -> None: ...

    def pump_events(self, timeout_ms: int = 25) -> None: ...


def _classify_glib_error(exc: Exception) -> FprintError:
    text = str(exc)
    dbus_name: str | None = None
    try:
        from gi.repository import Gio

        dbus_name = Gio.dbus_error_get_remote_error(exc)
    except Exception:
        dbus_name = None

    haystack = f"{dbus_name or ''} {text}".lower()
    if "nosuchdevice" in haystack or "no devices" in haystack or "serviceunknown" in haystack:
        return FprintError("no_device", text, dbus_name)
    if "alreadyclaimed" in haystack or "already in use" in haystack:
        return FprintError("already_claimed", text, dbus_name)
    if "noenrolledprints" in haystack or "no fingerprints enrolled" in haystack:
        return FprintError("no_enrolled_prints", text, dbus_name)
    if "permissiondenied" in haystack or "not authorized" in haystack or "accessdenied" in haystack:
        return FprintError("permission_denied", text, dbus_name)
    if "invalid" in haystack and "finger" in haystack:
        return FprintError("invalid_finger", text, dbus_name)
    if "claimdevice" in haystack:
        return FprintError("not_claimed", text, dbus_name)
    return FprintError("dbus_error", text, dbus_name)


class DBusFprintBackend:
    """Real fprintd client speaking net.reactivated.Fprint over Gio.DBus.

    By default connects to the system bus. Set ``FTE3600_TOOLKIT_DBUS_BUS=session``
    (or pass ``bus_type="session"``) to target an isolated ``dbus-run-session``
    bus for protocol-level E2E tests.
    """

    def __init__(
        self,
        *,
        bus_type: str | None = None,
        bus_name: str | None = None,
        call_timeout_ms: int = 30000,
    ) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib

        self._Gio = Gio
        self._GLib = GLib
        self._bus_name = bus_name or os.environ.get(
            "FTE3600_TOOLKIT_DBUS_NAME", FPRINTD_BUS_NAME
        )
        self._timeout_ms = call_timeout_ms
        resolved_bus = (
            bus_type
            or os.environ.get("FTE3600_TOOLKIT_DBUS_BUS", "system")
        ).lower()
        gio_bus = (
            Gio.BusType.SESSION
            if resolved_bus == "session"
            else Gio.BusType.SYSTEM
        )
        try:
            self._bus = Gio.bus_get_sync(gio_bus, None)
        except GLib.Error as exc:
            raise _classify_glib_error(exc) from exc
        # Dedicated GMainContext keeps D-Bus signal dispatch isolated from
        # GLib.MainContext.default() so GTK4's main loop and worker threads
        # never contend or execute UI callbacks on the wrong thread.
        self._ctx = GLib.MainContext.new()
        self._device_path: str | None = None

    def _call(
        self,
        path: str,
        iface: str,
        method: str,
        params=None,
        *,
        timeout_ms: int | None = None,
        allow_interactive_auth: bool = False,
    ):
        flags = self._Gio.DBusCallFlags.NONE
        if allow_interactive_auth:
            flags = getattr(
                self._Gio.DBusCallFlags,
                "ALLOW_INTERACTIVE_AUTHORIZATION",
                self._Gio.DBusCallFlags.NONE,
            )
        try:
            return self._bus.call_sync(
                self._bus_name,
                path,
                iface,
                method,
                params,
                None,
                flags,
                timeout_ms if timeout_ms is not None else self._timeout_ms,
                None,
            )
        except self._GLib.Error as exc:
            raise _classify_glib_error(exc) from exc

    def _resolve_device_path(self) -> str:
        if self._device_path is not None:
            return self._device_path
        res = self._call(
            FPRINTD_MANAGER_PATH,
            FPRINTD_MANAGER_IFACE,
            "GetDefaultDevice",
            None,
        )
        path = res.unpack()[0]
        if not path:
            raise FprintError("no_device", "fprintd returned no default device")
        self._device_path = str(path)
        return self._device_path

    def list_enrolled_fingers(self, username: str = "") -> list[str]:
        path = self._resolve_device_path()
        try:
            res = self._call(
                path,
                FPRINTD_DEVICE_IFACE,
                "ListEnrolledFingers",
                self._GLib.Variant("(s)", (username,)),
            )
            return list(res.unpack()[0])
        except FprintError as exc:
            if exc.code == "no_enrolled_prints":
                return []
            raise

    def get_default_device(self, username: str = "") -> DeviceInfo:
        path = self._resolve_device_path()
        props_variant = self._call(
            path,
            DBUS_PROPS_IFACE,
            "GetAll",
            self._GLib.Variant("(s)", (FPRINTD_DEVICE_IFACE,)),
        )
        props = props_variant.unpack()[0]
        name = str(props.get("name", "Unknown Fingerprint Reader"))
        scan_type = str(props.get("scan-type", "unknown"))
        num_stages = int(props.get("num-enroll-stages", 0))
        fingers = tuple(self.list_enrolled_fingers(username))
        return DeviceInfo(
            object_path=path,
            name=name,
            scan_type=scan_type,
            num_enroll_stages=num_stages,
            enrolled_fingers=fingers,
        )

    def delete_enrolled_finger(self, finger: str, username: str = "") -> None:
        path = self._resolve_device_path()
        self.claim(username)
        try:
            self._call(
                path,
                FPRINTD_DEVICE_IFACE,
                "DeleteEnrolledFinger",
                self._GLib.Variant("(s)", (finger,)),
                timeout_ms=max(self._timeout_ms, 120000),
                allow_interactive_auth=True,
            )
        finally:
            try:
                self.release()
            except FprintError:
                pass

    def delete_enrolled_fingers(self, username: str = "") -> None:
        path = self._resolve_device_path()
        self._call(
            path,
            FPRINTD_DEVICE_IFACE,
            "DeleteEnrolledFingers",
            self._GLib.Variant("(s)", (username,)),
            timeout_ms=max(self._timeout_ms, 120000),
            allow_interactive_auth=True,
        )

    def claim(self, username: str = "") -> None:
        path = self._resolve_device_path()
        # Claim wakes up FTE3600 (~4-5 s, up to ~10 s with cold-open retry) and
        # may trigger a Polkit dialog when targeting another username.
        self._call(
            path,
            FPRINTD_DEVICE_IFACE,
            "Claim",
            self._GLib.Variant("(s)", (username,)),
            timeout_ms=max(self._timeout_ms, 120000),
            allow_interactive_auth=True,
        )

    def release(self) -> None:
        if self._device_path is None:
            return
        self._call(
            self._device_path,
            FPRINTD_DEVICE_IFACE,
            "Release",
            None,
            timeout_ms=15000,
        )

    def verify_start(self, finger: str) -> None:
        path = self._resolve_device_path()
        self._call(
            path,
            FPRINTD_DEVICE_IFACE,
            "VerifyStart",
            self._GLib.Variant("(s)", (finger,)),
        )

    def verify_stop(self) -> None:
        if self._device_path is None:
            return
        self._call(
            self._device_path,
            FPRINTD_DEVICE_IFACE,
            "VerifyStop",
            None,
            timeout_ms=15000,
        )

    def enroll_start(self, finger: str) -> None:
        path = self._resolve_device_path()
        self._call(
            path,
            FPRINTD_DEVICE_IFACE,
            "EnrollStart",
            self._GLib.Variant("(s)", (finger,)),
            timeout_ms=max(self._timeout_ms, 120000),
            allow_interactive_auth=True,
        )

    def enroll_stop(self) -> None:
        if self._device_path is None:
            return
        self._call(
            self._device_path,
            FPRINTD_DEVICE_IFACE,
            "EnrollStop",
            None,
            timeout_ms=15000,
        )

    def subscribe_signals(
        self,
        on_verify_status: Callable[[str, bool], None],
        on_enroll_status: Callable[[str, bool], None],
    ) -> int:
        path = self._resolve_device_path()

        def _dispatch(_conn, _sender, _obj_path, _iface, signal_name, params):
            if signal_name == "VerifyStatus":
                result, done = params.unpack()
                on_verify_status(str(result), bool(done))
            elif signal_name == "EnrollStatus":
                result, done = params.unpack()
                on_enroll_status(str(result), bool(done))

        self._ctx.push_thread_default()
        try:
            return int(
                self._bus.signal_subscribe(
                    self._bus_name,
                    FPRINTD_DEVICE_IFACE,
                    None,
                    path,
                    None,
                    self._Gio.DBusSignalFlags.NONE,
                    _dispatch,
                )
            )
        finally:
            self._ctx.pop_thread_default()

    def unsubscribe_signals(self, subscription_id: int) -> None:
        if subscription_id:
            self._bus.signal_unsubscribe(subscription_id)

    def pump_events(self, timeout_ms: int = 25) -> None:
        ctx = self._ctx
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
        while True:
            did_work = False
            while ctx.pending():
                ctx.iteration(False)
                did_work = True
            if did_work or time.monotonic() >= deadline:
                break
            time.sleep(0.005)
