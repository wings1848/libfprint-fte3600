# SPDX-License-Identifier: LGPL-2.1-or-later
"""Protocol-level D-Bus E2E tests against mock_fprintd.py over a real D-Bus daemon.

Run inside ``dbus-run-session`` (or with ``DBUS_SESSION_BUS_ADDRESS`` active):
  dbus-run-session -- python3 -m unittest toolkit/tests/test_dbus_e2e.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

TOOLKIT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = TOOLKIT_ROOT / "src"
MOCK_SCRIPT = TOOLKIT_ROOT / "tests" / "mock_fprintd.py"


class MockBusFixture:
    """Starts mock_fprintd.py on the current session bus and tears it down cleanly."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._tmp = tempfile.TemporaryDirectory()
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "MockBusFixture":
        ready_file = Path(self._tmp.name) / "ready"
        self._proc = subprocess.Popen(
            [
                sys.executable,
                str(MOCK_SCRIPT),
                "--config-json",
                json.dumps(self._config),
                "--ready-file",
                str(ready_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if ready_file.exists():
                return self
            if self._proc.poll() is not None:
                out, err = self._proc.communicate(timeout=2)
                raise RuntimeError(f"mock_fprintd exited early: {out}\n{err}")
            time.sleep(0.02)
        raise TimeoutError("Timed out waiting for mock_fprintd to acquire D-Bus name")

    def get_stats(self) -> dict:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync(
            "net.reactivated.Fprint",
            "/net/reactivated/Fprint/Device/0",
            "net.reactivated.Fprint.Device",
            "GetStats",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return json.loads(res.unpack()[0])

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)
            if self._proc.stdout is not None:
                self._proc.stdout.close()
            if self._proc.stderr is not None:
                self._proc.stderr.close()
        self._tmp.cleanup()


def _cli_env(state_file: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["FTE3600_TOOLKIT_DBUS_BUS"] = "session"
    env["FTE3600_TOOLKIT_DISABLE_LOGIND"] = "1"
    env["FTE3600_TOOLKIT_STATE_FILE"] = str(state_file)
    return env


def _run_cli(args: list[str], state_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "fte3600_toolkit.cli.main", *args],
        env=_cli_env(state_file),
        capture_output=True,
        text=True,
        timeout=30,
    )


@unittest.skipUnless(
    os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
    "Requires an active session D-Bus (run under dbus-run-session)",
)
class DBusEndToEndTests(unittest.TestCase):
    def test_status_verify_five_rounds_and_report_arithmetic(self) -> None:
        config = {
            "name": "FocalTech FTE3600 (host BRISK)",
            "scan_type": "press",
            "num_enroll_stages": 9,
            "claim_delay_ms": 180,
            "signal_delay_ms": 25,
            "enrolled": ["right-index-finger"],
            "verify_scripts": [
                [["verify-match", True, 25]],
                [["verify-retry-scan", False, 20], ["verify-match", True, 45]],
                [["verify-no-match", True, 25]],
                [["verify-match", True, 25]],
                [["verify-unknown-error", True, 25]],
            ],
        }
        with tempfile.TemporaryDirectory() as tmp, MockBusFixture(config) as mock:
            state_file = Path(tmp) / "rounds.jsonl"

            # 1. status --json
            st = _run_cli(["--json", "status"], state_file)
            self.assertEqual(st.returncode, 0, st.stderr)
            info = json.loads(st.stdout)
            self.assertIn("FocalTech FTE3600", info["name"])
            self.assertEqual(info["scan_type"], "press")
            self.assertEqual(info["num_enroll_stages"], 9)

            # 2. verify --rounds 5 --json
            ver = _run_cli(
                [
                    "--json",
                    "--no-sound",
                    "--no-notify",
                    "--no-power-inhibit",
                    "verify",
                    "--rounds",
                    "5",
                    "--gap",
                    "0",
                    "--finger",
                    "right-index-finger",
                ],
                state_file,
            )
            # Partial match (3/5) -> exit code 1 per contract.
            self.assertEqual(ver.returncode, 1, ver.stderr)
            events = [json.loads(line) for line in ver.stdout.splitlines() if line.strip()]
            ready_events = [e for e in events if e.get("event") == "ready"]
            self.assertEqual(len(ready_events), 5)
            # Contract invariant: ready never fires before simulated 180ms Claim delay.
            for ev in ready_events:
                self.assertGreaterEqual(ev["elapsed_ms"], 160)

            summary_ev = [e for e in events if e.get("summary")][-1]
            self.assertEqual(summary_ev["matches"], 3)
            self.assertEqual(summary_ev["total"], 5)

            # 3. rounds.jsonl grew by exact 5 lines.
            lines = [ln for ln in state_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(lines), 5)

            # 4. report --json arithmetic matches rounds.jsonl.
            rep = _run_cli(["--json", "report"], state_file)
            self.assertEqual(rep.returncode, 0, rep.stderr)
            report = json.loads(rep.stdout)
            self.assertEqual(report["total"], 5)
            self.assertEqual(report["matches"], 3)
            self.assertAlmostEqual(report["match_rate"], 0.6)
            self.assertEqual(
                report["histogram"],
                {"verify-match": 3, "verify-no-match": 1, "verify-unknown-error": 1},
            )

            stats = mock.get_stats()
            self.assertFalse(stats["claimed"])
            self.assertEqual(stats["claim_calls"], 5)
            self.assertEqual(stats["release_calls"], 5)

    def test_sigint_releases_device_immediately(self) -> None:
        # Empty verify script hangs waiting for a finger until stopped.
        config = {
            "claim_delay_ms": 20,
            "enrolled": ["right-index-finger"],
            "verify_scripts": [[]],
        }
        with tempfile.TemporaryDirectory() as tmp, MockBusFixture(config) as mock:
            state_file = Path(tmp) / "rounds.jsonl"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "fte3600_toolkit.cli.main",
                    "--no-sound",
                    "--no-notify",
                    "--no-power-inhibit",
                    "verify",
                    "--rounds",
                    "1",
                    "--timeout",
                    "30",
                ],
                env=_cli_env(state_file),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            saw_ready = False
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                if "READY:" in line:
                    saw_ready = True
                    break
            self.assertTrue(saw_ready, "CLI never emitted READY before SIGINT")

            proc.send_signal(signal.SIGINT)
            rc = proc.wait(timeout=5.0)
            proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            self.assertEqual(rc, 130)

            # Verify mock fprintd received VerifyStop + Release and is not left claimed.
            stats = mock.get_stats()
            self.assertFalse(stats["claimed"])
            self.assertGreaterEqual(stats["verify_stop_calls"], 1)
            self.assertGreaterEqual(stats["release_calls"], 1)

    def test_enroll_list_delete_and_error_codes(self) -> None:
        config = {
            "num_enroll_stages": 9,
            "enrolled": ["right-index-finger"],
            "enroll_script": [
                ["enroll-stage-passed", False, 15],
                ["enroll-retry-scan", False, 30],
                ["enroll-stage-passed", False, 45],
                ["enroll-completed", True, 60],
            ],
        }
        with tempfile.TemporaryDirectory() as tmp, MockBusFixture(config) as mock:
            state_file = Path(tmp) / "rounds.jsonl"
            enr = _run_cli(
                [
                    "--json",
                    "--no-sound",
                    "--no-notify",
                    "--no-power-inhibit",
                    "enroll",
                    "--finger",
                    "left-index-finger",
                ],
                state_file,
            )
            self.assertEqual(enr.returncode, 0, enr.stderr)

            lst = _run_cli(["--json", "list"], state_file)
            self.assertEqual(lst.returncode, 0)
            self.assertEqual(
                sorted(json.loads(lst.stdout)["fingers"]),
                ["left-index-finger", "right-index-finger"],
            )

            dele = _run_cli(["--json", "delete", "--finger", "left-index-finger"], state_file)
            self.assertEqual(dele.returncode, 0)
            self.assertEqual(mock.get_stats()["enrolled"], ["right-index-finger"])

        # AlreadyClaimed error -> exit code 2
        with tempfile.TemporaryDirectory() as tmp, MockBusFixture(
            {"already_claimed": True, "enrolled": ["right-index-finger"]}
        ):
            state_file = Path(tmp) / "rounds.jsonl"
            res = _run_cli(
                ["--json", "--no-sound", "--no-notify", "--no-power-inhibit", "verify", "--rounds", "1"],
                state_file,
            )
            self.assertEqual(res.returncode, 2)

        # NoSuchDevice -> status returns exit code 2
        with tempfile.TemporaryDirectory() as tmp, MockBusFixture({"no_device": True}):
            state_file = Path(tmp) / "rounds.jsonl"
            res = _run_cli(["--json", "status"], state_file)
            self.assertEqual(res.returncode, 2)

    def test_worker_thread_verify_while_default_main_loop_runs(self) -> None:
        """Reproduces the GTK4 GUI model: GLib.MainLoop owns default context on main thread."""
        import threading
        from gi.repository import GLib

        sys.path.insert(0, str(SRC_DIR))
        from fte3600_toolkit.core import DBusFprintBackend, run_verify_rounds

        config = {
            "claim_delay_ms": 40,
            "enrolled": ["right-index-finger"],
            "verify_scripts": [[["verify-match", True, 30]]],
        }
        with MockBusFixture(config) as mock:
            loop = GLib.MainLoop()
            outcome: dict[str, object] = {}

            def _worker() -> None:
                try:
                    backend = DBusFprintBackend(bus_type="session")
                    res = run_verify_rounds(
                        backend,
                        rounds=1,
                        gap_seconds=0.0,
                        inhibit_power_key=False,
                    )
                    outcome["result"] = res
                except Exception as exc:  # pragma: no cover
                    outcome["error"] = exc
                finally:
                    GLib.idle_add(loop.quit)

            # Safety guard so a deadlock fails fast instead of hanging CI.
            timeout_id = GLib.timeout_add(5000, loop.quit)
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            loop.run()
            GLib.source_remove(timeout_id)
            t.join(timeout=2.0)

            self.assertNotIn("error", outcome, outcome.get("error"))
            self.assertIn("result", outcome, "Worker thread timed out / deadlocked against default GLib.MainLoop")
            res = outcome["result"]
            self.assertEqual(res.exit_code, 0)
            self.assertEqual(res.matches, 1)
            stats = mock.get_stats()
            self.assertEqual(stats["claim_calls"], 1)
            self.assertEqual(stats["release_calls"], 1)


if __name__ == "__main__":
    unittest.main()
