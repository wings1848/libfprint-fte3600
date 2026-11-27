# SPDX-License-Identifier: LGPL-2.1-or-later
"""Unit tests for toolkit core: vocabulary, JSONL store, power/doctor, and state machine."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fte3600_toolkit.core import (  # noqa: E402
    DeviceInfo,
    FprintError,
    RecordStore,
    RoundRecord,
    SessionEvent,
    explain_result,
    inspect_powerdevil_config,
    resolve_language,
    run_doctor,
    run_enroll,
    run_verify_rounds,
    summarize_records,
    ui_text,
)


class FakeBackend:
    """In-memory deterministic FprintBackend for unit testing the state machine."""

    def __init__(
        self,
        *,
        claim_delay: float = 0.0,
        verify_sequences: list[list[tuple[str, bool]]] | None = None,
        enroll_sequence: list[tuple[str, bool]] | None = None,
        num_stages: int = 9,
        fail_on_claim: FprintError | None = None,
    ) -> None:
        self.claim_delay = claim_delay
        self.verify_sequences = list(verify_sequences or [[("verify-match", True)]])
        self.enroll_sequence = list(enroll_sequence or [])
        self.num_stages = num_stages
        self.fail_on_claim = fail_on_claim
        self.calls: list[str] = []
        self.claimed = False
        self._verify_cb = None
        self._enroll_cb = None
        self._pending: list[tuple[str, str, bool]] = []
        self._verify_idx = 0

    def get_default_device(self, username: str = "") -> DeviceInfo:
        self.calls.append("get_default_device")
        return DeviceInfo(
            object_path="/net/reactivated/Fprint/Device/0",
            name="FocalTech FTE3600 (Fake)",
            scan_type="press",
            num_enroll_stages=self.num_stages,
            enrolled_fingers=("right-index-finger",),
        )

    def list_enrolled_fingers(self, username: str = "") -> list[str]:
        return ["right-index-finger"]

    def delete_enrolled_finger(self, finger: str, username: str = "") -> None:
        self.calls.append(f"delete:{finger}")

    def delete_enrolled_fingers(self, username: str = "") -> None:
        self.calls.append("delete_all")

    def claim(self, username: str = "") -> None:
        self.calls.append("claim")
        if self.fail_on_claim is not None:
            raise self.fail_on_claim
        if self.claim_delay > 0:
            time.sleep(self.claim_delay)
        self.claimed = True

    def release(self) -> None:
        self.calls.append("release")
        self.claimed = False

    def verify_start(self, finger: str) -> None:
        self.calls.append(f"verify_start:{finger}")
        seq = (
            self.verify_sequences[self._verify_idx]
            if self._verify_idx < len(self.verify_sequences)
            else [("verify-match", True)]
        )
        self._verify_idx += 1
        for res, done in seq:
            self._pending.append(("verify", res, done))

    def verify_stop(self) -> None:
        self.calls.append("verify_stop")
        self._pending.clear()

    def enroll_start(self, finger: str) -> None:
        self.calls.append(f"enroll_start:{finger}")
        for res, done in self.enroll_sequence:
            self._pending.append(("enroll", res, done))

    def enroll_stop(self) -> None:
        self.calls.append("enroll_stop")
        self._pending.clear()

    def subscribe_signals(self, on_verify_status, on_enroll_status) -> int:
        self._verify_cb = on_verify_status
        self._enroll_cb = on_enroll_status
        return 1

    def unsubscribe_signals(self, subscription_id: int) -> None:
        self._verify_cb = None
        self._enroll_cb = None

    def pump_events(self, timeout_ms: int = 25) -> None:
        if self._pending:
            kind, res, done = self._pending.pop(0)
            if kind == "verify" and self._verify_cb:
                self._verify_cb(res, done)
            elif kind == "enroll" and self._enroll_cb:
                self._enroll_cb(res, done)
        else:
            time.sleep(min(0.01, timeout_ms / 1000.0))


class VocabularyTests(unittest.TestCase):
    def test_known_and_unknown_results(self) -> None:
        ok = explain_result("verify-match", "en")
        self.assertTrue(ok.matched)
        self.assertEqual(ok.kind, "ok")

        miss_zh = explain_result("verify-no-match", "zh")
        self.assertFalse(miss_zh.matched)
        self.assertIn("按实", miss_zh.guidance)

        unknown = explain_result("verify-future-code", "en")
        self.assertEqual(unknown.kind, "error")
        self.assertEqual(unknown.code, "verify-future-code")

    def test_language_resolution(self) -> None:
        self.assertEqual(resolve_language("en"), "en")
        self.assertEqual(resolve_language("zh"), "zh")
        self.assertEqual(resolve_language("fr"), "en")

    def test_64x80_enroll_guidance_keeps_angle_and_overlap(self) -> None:
        for code in ("enroll-stage-passed", "enroll-retry-scan", "enroll-remove-and-retry"):
            en = explain_result(code, "en")
            self.assertIn("same finger angle", en.guidance.lower())
            self.assertIn("70% overlap", en.guidance)
            self.assertNotIn("angle shift", en.guidance.lower())

            zh = explain_result(code, "zh")
            self.assertIn("保持相同角度", zh.guidance)
            self.assertIn("70%", zh.guidance)
            self.assertNotIn("调整角度", zh.guidance)

        stage_en = ui_text("stage_progress", "en", stage=2, total_stages=9)
        self.assertIn("2 of 9", stage_en)
        self.assertIn("keep angle", stage_en)
        self.assertIn("70% overlap", stage_en)

        stage_zh = ui_text("stage_progress", "zh", stage=2, total_stages=9)
        self.assertIn("2/9", stage_zh)
        self.assertIn("保持角度", stage_zh)
        self.assertIn("70%", stage_zh)


class StoreTests(unittest.TestCase):
    def test_append_load_and_summary_arithmetic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rounds.jsonl"
            store = RecordStore(path)
            records = [
                RoundRecord.now(mode="verify", round_num=1, finger="any", result="verify-match", claim_ms=4000, ready_ms=4050, total_ms=5200),
                RoundRecord.now(mode="verify", round_num=2, finger="any", result="verify-no-match", claim_ms=4200, ready_ms=4250, total_ms=6100),
                RoundRecord.now(mode="verify", round_num=3, finger="any", result="verify-match", claim_ms=4100, ready_ms=4150, total_ms=5500),
            ]
            for rec in records:
                store.append(rec)
            # Inject one corrupted line to confirm resilient parsing.
            with path.open("a", encoding="utf-8") as fh:
                fh.write("{not-valid-json\n")

            loaded, malformed = store.load()
            self.assertEqual(len(loaded), 3)
            self.assertEqual(malformed, 1)

            summary = store.summarize(mode="verify")
            self.assertEqual(summary.total, 3)
            self.assertEqual(summary.matches, 2)
            self.assertAlmostEqual(summary.match_rate, 2 / 3)
            self.assertEqual(summary.median_claim_ms, 4100.0)
            self.assertEqual(summary.histogram, {"verify-match": 2, "verify-no-match": 1})
            self.assertEqual(summary.malformed_lines, 1)

            # Privacy invariant: serialized JSONL must never contain image/template keys.
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("pixels", raw)
            self.assertNotIn("template", raw)


class PowerAndDoctorTests(unittest.TestCase):
    def test_powerdevil_parser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "powerdevilrc"
            cfg.write_text(
                "[AC][SuspendAndShutdown]\nPowerButtonAction=0\n\n"
                "[Battery][SuspendAndShutdown]\nPowerButtonAction=8\n\n"
                "[LowBattery][SuspendAndShutdown]\nPowerButtonAction=0\n",
                encoding="utf-8",
            )
            status = inspect_powerdevil_config(cfg)
            self.assertTrue(status.config_present)
            self.assertTrue(status.may_trigger_power_menu)

            cfg.write_text(
                "[AC][SuspendAndShutdown]\nPowerButtonAction=0\n"
                "[Battery][SuspendAndShutdown]\nPowerButtonAction=0\n"
                "[LowBattery][SuspendAndShutdown]\nPowerButtonAction=0\n",
                encoding="utf-8",
            )
            self.assertFalse(inspect_powerdevil_config(cfg).may_trigger_power_menu)

    def _make_synthetic_sysfs(
        self,
        root: Path,
        *,
        glue_status: str | None = "ready",
        with_dev_nodes: bool = True,
    ) -> tuple[Path, Path]:
        sysfs = root / "sys"
        dev = root / "dev"
        (sysfs / "bus/acpi/devices/FTE3600:00").mkdir(parents=True)
        (sysfs / "bus/acpi/devices/FTE3600:00/status").write_text("15\n")
        if glue_status is not None:
            glue = sysfs / "bus/platform/devices/fte3600-glue.1.auto"
            glue.mkdir(parents=True)
            (glue / "fte3600_glue_abi").write_text("2\n")
            (glue / "fte3600_status").write_text(f"{glue_status}\n")
            (glue / "fte3600_acpi_speed_hz").write_text("1000000\n")
            (glue / "fte3600_cs_control").write_text("1\n")
            (glue / "fte3600_irq_source").write_text("gpio\n")
        (sysfs / "module/spidev/parameters").mkdir(parents=True)
        (sysfs / "module/spidev/parameters/bufsiz").write_text("65536\n")
        dev.mkdir(parents=True)
        if with_dev_nodes:
            for name in ("fte3600-spi-FTE3600:00", "fte3600-gpio-FTE3600:00", "fte3600-irq-FTE3600:00"):
                (dev / name).write_text("")
        return sysfs, dev

    def test_doctor_on_synthetic_sysfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sysfs, dev = self._make_synthetic_sysfs(root)

            checks = run_doctor(
                backend=FakeBackend(),
                sysfs_root=sysfs,
                dev_root=dev,
                powerdevil_config=root / "missing-powerdevilrc",
            )
            by_name = {c.name: c for c in checks}
            self.assertEqual(by_name["acpi"].status, "ok")
            self.assertEqual(by_name["kernel-glue"].status, "ok")
            self.assertEqual(by_name["spidev-bufsiz"].status, "ok")
            self.assertEqual(by_name["dev-nodes"].status, "ok")
            self.assertEqual(by_name["fprintd"].status, "ok")

    def test_doctor_dev_nodes_warn_vs_fail_semantics(self) -> None:
        class FailingBackend(FakeBackend):
            def get_default_device(self, username: str = "") -> DeviceInfo:
                raise FprintError("no_such_device", "No fingerprint device available")

        # 1. Local-dev: kernel-glue ok + fprintd ok, but /dev/fte3600-* missing -> warn
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sysfs, dev = self._make_synthetic_sysfs(root, with_dev_nodes=False)
            checks = run_doctor(
                backend=FakeBackend(),
                sysfs_root=sysfs,
                dev_root=dev,
                powerdevil_config=root / "missing-powerdevilrc",
            )
            by_name = {c.name: c for c in checks}
            self.assertEqual(by_name["kernel-glue"].status, "ok")
            self.assertEqual(by_name["fprintd"].status, "ok")
            self.assertEqual(by_name["dev-nodes"].status, "warn")
            self.assertIn("sysfs", by_name["dev-nodes"].hint.lower())
            self.assertIn("udev", by_name["dev-nodes"].hint.lower())

        # 2. kernel-glue NOT ok + fprintd ok, /dev/fte3600-* missing -> fail
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sysfs, dev = self._make_synthetic_sysfs(root, glue_status="error", with_dev_nodes=False)
            checks = run_doctor(
                backend=FakeBackend(),
                sysfs_root=sysfs,
                dev_root=dev,
                powerdevil_config=root / "missing-powerdevilrc",
            )
            by_name = {c.name: c for c in checks}
            self.assertEqual(by_name["kernel-glue"].status, "fail")
            self.assertEqual(by_name["fprintd"].status, "ok")
            self.assertEqual(by_name["dev-nodes"].status, "fail")

        # 3. kernel-glue ok + fprintd NOT ok, /dev/fte3600-* missing -> fail
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sysfs, dev = self._make_synthetic_sysfs(root, glue_status="ready", with_dev_nodes=False)
            checks = run_doctor(
                backend=FailingBackend(),
                sysfs_root=sysfs,
                dev_root=dev,
                powerdevil_config=root / "missing-powerdevilrc",
            )
            by_name = {c.name: c for c in checks}
            self.assertEqual(by_name["kernel-glue"].status, "ok")
            self.assertEqual(by_name["fprintd"].status, "fail")
            self.assertEqual(by_name["dev-nodes"].status, "fail")


class SessionStateMachineTests(unittest.TestCase):
    def test_ready_strictly_follows_claim_and_verify_start(self) -> None:
        backend = FakeBackend(
            claim_delay=0.05,
            verify_sequences=[
                [("verify-retry-scan", False), ("verify-match", True)],
                [("verify-no-match", True)],
            ],
        )
        events: list[SessionEvent] = []
        with tempfile.TemporaryDirectory() as tmp:
            store = RecordStore(Path(tmp) / "rounds.jsonl")
            result = run_verify_rounds(
                backend,
                finger="right-index-finger",
                rounds=2,
                gap_seconds=0.0,
                store=store,
                inhibit_power_key=False,
                on_event=events.append,
            )
            self.assertEqual(result.total, 2)
            self.assertEqual(result.matches, 1)
            self.assertEqual(result.exit_code, 1)
            self.assertFalse(backend.claimed)
            states = [e.state for e in events]
            self.assertEqual(
                states,
                ["opening", "ready", "retry", "verdict", "opening", "ready", "verdict"],
            )
            # First ready must be strictly after the 50ms claim delay.
            ready_events = [e for e in events if e.state == "ready"]
            self.assertGreaterEqual(ready_events[0].elapsed_ms, 45)
            # Verify store grew by exact 2 lines.
            loaded, _ = store.load()
            self.assertEqual(len(loaded), 2)

    def test_enroll_uses_dynamic_num_stages_and_releases(self) -> None:
        backend = FakeBackend(
            num_stages=9,
            enroll_sequence=[
                ("enroll-stage-passed", False),
                ("enroll-retry-scan", False),
                ("enroll-stage-passed", False),
                ("enroll-completed", True),
            ],
        )
        events: list[SessionEvent] = []
        outcome = run_enroll(
            backend,
            finger="left-index-finger",
            inhibit_power_key=False,
            on_event=events.append,
        )
        self.assertTrue(outcome.completed)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.total_stages, 9)
        self.assertFalse(backend.claimed)
        self.assertIn("enroll_stop", backend.calls)
        self.assertIn("release", backend.calls)

    def test_cancel_releases_claimed_device(self) -> None:
        # Empty sequence means sensor never replies until cancelled.
        backend = FakeBackend(verify_sequences=[[]])
        stop = threading.Event()
        timer = threading.Timer(0.08, stop.set)
        timer.start()
        try:
            res = run_verify_rounds(
                backend,
                rounds=1,
                round_timeout_seconds=5.0,
                inhibit_power_key=False,
                stop_event=stop,
            )
        finally:
            timer.cancel()
        self.assertTrue(res.cancelled)
        self.assertEqual(res.exit_code, 130)
        self.assertFalse(backend.claimed)
        self.assertIn("verify_stop", backend.calls)
        self.assertIn("release", backend.calls)

    def test_device_error_returns_exit_code_2(self) -> None:
        backend = FakeBackend(
            fail_on_claim=FprintError("already_claimed", "Device was already claimed")
        )
        res = run_verify_rounds(backend, rounds=1, inhibit_power_key=False)
        self.assertEqual(res.exit_code, 2)
        self.assertIn("already_claimed", res.device_error or "")

    def test_cli_accepts_global_flags_before_or_after_subcommand(self) -> None:
        from fte3600_toolkit.cli.main import _build_parser

        parser = _build_parser()
        a = parser.parse_args(["--json", "--lang", "zh", "--no-sound", "status"])
        b = parser.parse_args(["status", "--json", "--lang", "zh", "--no-sound"])
        c = parser.parse_args(["--json", "verify", "--rounds", "3", "--no-notify"])
        self.assertTrue(a.json and b.json and c.json)
        self.assertEqual(a.lang, "zh")
        self.assertEqual(b.lang, "zh")
        self.assertTrue(a.no_sound and b.no_sound)
        self.assertTrue(c.no_notify)
        self.assertEqual(c.rounds, 3)


if __name__ == "__main__":
    unittest.main()
