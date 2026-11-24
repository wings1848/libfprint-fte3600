#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Real Gio.DBus mock fprintd service for protocol-level E2E testing.

Exports net.reactivated.Fprint.Manager and net.reactivated.Fprint.Device on
the session bus (inside ``dbus-run-session``) with configurable Claim latency,
scripted VerifyStatus/EnrollStatus signal streams, error injection, and call
counters for verifying VerifyStop + Release cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

BUS_NAME = "net.reactivated.Fprint"
MANAGER_PATH = "/net/reactivated/Fprint/Manager"
DEVICE_PATH = "/net/reactivated/Fprint/Device/0"

MANAGER_XML = """
<node>
  <interface name="net.reactivated.Fprint.Manager">
    <method name="GetDefaultDevice">
      <arg type="o" name="device" direction="out"/>
    </method>
    <method name="GetDevices">
      <arg type="ao" name="devices" direction="out"/>
    </method>
  </interface>
</node>
"""

DEVICE_XML = """
<node>
  <interface name="net.reactivated.Fprint.Device">
    <method name="ListEnrolledFingers">
      <arg type="s" name="username" direction="in"/>
      <arg type="as" name="enrolled_fingers" direction="out"/>
    </method>
    <method name="DeleteEnrolledFingers">
      <arg type="s" name="username" direction="in"/>
    </method>
    <method name="DeleteEnrolledFinger">
      <arg type="s" name="finger_name" direction="in"/>
    </method>
    <method name="Claim">
      <arg type="s" name="username" direction="in"/>
    </method>
    <method name="Release"/>
    <method name="VerifyStart">
      <arg type="s" name="finger_name" direction="in"/>
    </method>
    <method name="VerifyStop"/>
    <method name="EnrollStart">
      <arg type="s" name="finger_name" direction="in"/>
    </method>
    <method name="EnrollStop"/>
    <method name="GetStats">
      <arg type="s" name="stats_json" direction="out"/>
    </method>
    <signal name="VerifyStatus">
      <arg type="s" name="result"/>
      <arg type="b" name="done"/>
    </signal>
    <signal name="EnrollStatus">
      <arg type="s" name="result"/>
      <arg type="b" name="done"/>
    </signal>
    <property name="name" type="s" access="read"/>
    <property name="scan-type" type="s" access="read"/>
    <property name="num-enroll-stages" type="i" access="read"/>
  </interface>
</node>
"""


class MockFprintdService:
    """In-process Gio.DBus implementation of net.reactivated.Fprint."""

    def __init__(self, config: dict) -> None:
        self.name = str(config.get("name", "FocalTech FTE3600 (Mock)"))
        self.scan_type = str(config.get("scan_type", "press"))
        self.num_enroll_stages = int(config.get("num_enroll_stages", 9))
        self.claim_delay_ms = int(config.get("claim_delay_ms", 0))
        self.signal_delay_ms = int(config.get("signal_delay_ms", 30))
        self.no_device = bool(config.get("no_device", False))
        self.already_claimed_error = bool(config.get("already_claimed", False))
        self.enrolled: list[str] = list(config.get("enrolled", ["right-index-finger"]))
        self.verify_scripts: list[list[list]] = list(config.get("verify_scripts", []))
        self.enroll_script: list[list] = list(config.get("enroll_script", []))

        self.claimed = False
        self.verify_active = False
        self.enroll_active = False
        self.active_finger = ""
        self.verify_round_idx = 0
        self.pending_timers: list[int] = []
        self.stats = {
            "claim_calls": 0,
            "release_calls": 0,
            "verify_start_calls": 0,
            "verify_stop_calls": 0,
            "enroll_start_calls": 0,
            "enroll_stop_calls": 0,
            "delete_calls": 0,
        }
        self._conn: Gio.DBusConnection | None = None

    def _cancel_timers(self) -> None:
        for tid in self.pending_timers:
            GLib.source_remove(tid)
        self.pending_timers.clear()

    def _emit(self, signal_name: str, result: str, done: bool) -> None:
        if self._conn is None:
            return
        self._conn.emit_signal(
            None,
            DEVICE_PATH,
            "net.reactivated.Fprint.Device",
            signal_name,
            GLib.Variant("(sb)", (result, done)),
        )

    def _schedule_script(self, signal_name: str, script: list[list], is_enroll: bool) -> None:
        if not script:
            return
        for step_idx, item in enumerate(script):
            code = str(item[0])
            done = bool(item[1])
            delay = int(item[2]) if len(item) > 2 else self.signal_delay_ms * (step_idx + 1)

            def _fire(c=code, d=done, enroll_flag=is_enroll):
                if enroll_flag and not self.enroll_active:
                    return False
                if not enroll_flag and not self.verify_active:
                    return False
                if enroll_flag and c == "enroll-completed" and self.active_finger:
                    if self.active_finger not in self.enrolled:
                        self.enrolled.append(self.active_finger)
                self._emit(signal_name, c, d)
                if d:
                    if enroll_flag:
                        self.enroll_active = False
                    else:
                        self.verify_active = False
                return False

            tid = GLib.timeout_add(max(1, delay), _fire)
            self.pending_timers.append(tid)

    def handle_manager(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method == "GetDefaultDevice":
            if self.no_device:
                invocation.return_dbus_error(
                    "net.reactivated.Fprint.Error.NoSuchDevice",
                    "No devices available",
                )
            else:
                invocation.return_value(GLib.Variant("(o)", (DEVICE_PATH,)))
        elif method == "GetDevices":
            devices = [] if self.no_device else [DEVICE_PATH]
            invocation.return_value(GLib.Variant("(ao)", (devices,)))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def handle_device(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "ListEnrolledFingers":
            if not self.enrolled:
                invocation.return_dbus_error(
                    "net.reactivated.Fprint.Error.NoEnrolledPrints",
                    "User has no enrolled prints",
                )
            else:
                invocation.return_value(GLib.Variant("(as)", (self.enrolled,)))
        elif method == "DeleteEnrolledFingers":
            self.stats["delete_calls"] += 1
            self.enrolled.clear()
            invocation.return_value(None)
        elif method == "DeleteEnrolledFinger":
            self.stats["delete_calls"] += 1
            finger = params.unpack()[0]
            if finger in self.enrolled:
                self.enrolled.remove(finger)
            invocation.return_value(None)
        elif method == "Claim":
            self.stats["claim_calls"] += 1
            if self.already_claimed_error or self.claimed:
                invocation.return_dbus_error(
                    "net.reactivated.Fprint.Error.AlreadyInUse",
                    "Device was already claimed",
                )
                return
            if self.claim_delay_ms > 0:
                time.sleep(self.claim_delay_ms / 1000.0)
            self.claimed = True
            invocation.return_value(None)
        elif method == "Release":
            self.stats["release_calls"] += 1
            self._cancel_timers()
            self.verify_active = False
            self.enroll_active = False
            self.claimed = False
            invocation.return_value(None)
        elif method == "VerifyStart":
            self.stats["verify_start_calls"] += 1
            if not self.claimed:
                invocation.return_dbus_error(
                    "net.reactivated.Fprint.Error.ClaimDevice",
                    "Client must claim device first",
                )
                return
            if not self.enrolled:
                invocation.return_dbus_error(
                    "net.reactivated.Fprint.Error.NoEnrolledPrints",
                    "No fingerprints enrolled",
                )
                return
            self.verify_active = True
            self.active_finger = params.unpack()[0]
            script = (
                self.verify_scripts[self.verify_round_idx]
                if self.verify_round_idx < len(self.verify_scripts)
                else [["verify-match", True, self.signal_delay_ms]]
            )
            self.verify_round_idx += 1
            invocation.return_value(None)
            self._schedule_script("VerifyStatus", script, is_enroll=False)
        elif method == "VerifyStop":
            self.stats["verify_stop_calls"] += 1
            self._cancel_timers()
            self.verify_active = False
            invocation.return_value(None)
        elif method == "EnrollStart":
            self.stats["enroll_start_calls"] += 1
            if not self.claimed:
                invocation.return_dbus_error(
                    "net.reactivated.Fprint.Error.ClaimDevice",
                    "Client must claim device first",
                )
                return
            self.enroll_active = True
            self.active_finger = params.unpack()[0]
            script = self.enroll_script or [
                ["enroll-stage-passed", False, 20],
                ["enroll-retry-scan", False, 40],
                ["enroll-stage-passed", False, 60],
                ["enroll-completed", True, 80],
            ]
            invocation.return_value(None)
            self._schedule_script("EnrollStatus", script, is_enroll=True)
        elif method == "EnrollStop":
            self.stats["enroll_stop_calls"] += 1
            self._cancel_timers()
            self.enroll_active = False
            invocation.return_value(None)
        elif method == "GetStats":
            payload = dict(self.stats)
            payload["claimed"] = self.claimed
            payload["verify_active"] = self.verify_active
            payload["enroll_active"] = self.enroll_active
            payload["enrolled"] = list(self.enrolled)
            invocation.return_value(GLib.Variant("(s)", (json.dumps(payload),)))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def handle_get_property(self, _conn, _sender, _path, _iface, prop_name):
        if prop_name == "name":
            return GLib.Variant("s", self.name)
        if prop_name == "scan-type":
            return GLib.Variant("s", self.scan_type)
        if prop_name == "num-enroll-stages":
            return GLib.Variant("i", self.num_enroll_stages)
        return None

    def register(self, conn: Gio.DBusConnection) -> None:
        self._conn = conn
        mgr_info = Gio.DBusNodeInfo.new_for_xml(MANAGER_XML).interfaces[0]
        dev_info = Gio.DBusNodeInfo.new_for_xml(DEVICE_XML).interfaces[0]
        conn.register_object(MANAGER_PATH, mgr_info, self.handle_manager, None, None)
        conn.register_object(
            DEVICE_PATH, dev_info, self.handle_device, self.handle_get_property, None
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-json", default="{}")
    parser.add_argument("--ready-file", default=None)
    args = parser.parse_args()

    config = json.loads(args.config_json)
    service = MockFprintdService(config)
    loop = GLib.MainLoop()

    def _on_bus_acquired(conn, _name):
        service.register(conn)

    def _on_name_acquired(_conn, _name):
        if args.ready_file:
            with open(args.ready_file, "w", encoding="utf-8") as fh:
                fh.write("ready\n")
        print("MOCK_FPRINTD_READY", flush=True)

    def _on_name_lost(_conn, _name):
        loop.quit()

    Gio.bus_own_name(
        Gio.BusType.SESSION,
        BUS_NAME,
        Gio.BusNameOwnerFlags.NONE,
        _on_bus_acquired,
        _on_name_acquired,
        _on_name_lost,
    )

    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    signal.signal(signal.SIGINT, lambda *_: loop.quit())
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
