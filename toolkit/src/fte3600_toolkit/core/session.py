# SPDX-License-Identifier: LGPL-2.1-or-later
"""Single shared enrollment & verification state machine for CLI and GUI.

Interaction contract:
  idle -> opening (Claim + Start) -> ready (prompt + sound) -> capturing/retry -> verdict
  "Press now" is NEVER emitted before Claim and VerifyStart/EnrollStart succeed.
  Cleanup ALWAYS calls Stop + Release and releases the power-key inhibitor.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable

from .backend import FprintBackend, FprintError
from .feedback import FeedbackChannel
from .power import PowerKeyInhibitor
from .store import RecordStore, RoundRecord
from .vocabulary import explain_result, ui_text


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """Immutable state transition emitted to CLI or GUI subscribers."""

    state: str  # "opening", "ready", "retry", "stage", "verdict", "error"
    message: str
    guidance: str = ""
    round_index: int = 0
    total_rounds: int = 0
    stage: int = 0
    total_stages: int = 0
    result: str | None = None
    matched: bool = False
    elapsed_ms: int = 0
    record: RoundRecord | None = None


@dataclass(frozen=True, slots=True)
class VerifyCampaignResult:
    """Aggregated outcome of N verification rounds."""

    records: tuple[RoundRecord, ...]
    cancelled: bool = False
    device_error: str | None = None

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def matches(self) -> int:
        return sum(1 for r in self.records if r.result == "verify-match")

    @property
    def exit_code(self) -> int:
        if self.device_error is not None:
            return 2
        if self.cancelled:
            return 130
        if self.total > 0 and self.matches == self.total:
            return 0
        return 1


@dataclass(frozen=True, slots=True)
class EnrollOutcome:
    """Outcome of a single finger enrollment session."""

    completed: bool
    result: str
    stages_passed: int
    total_stages: int
    record: RoundRecord | None = None
    cancelled: bool = False
    device_error: str | None = None

    @property
    def exit_code(self) -> int:
        if self.device_error is not None:
            return 2
        if self.cancelled:
            return 130
        return 0 if self.completed else 1


EventCallback = Callable[[SessionEvent], None]


def _emit(cb: EventCallback | None, event: SessionEvent) -> None:
    if cb is not None:
        cb(event)


def _ms_since(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _sleep_interruptible(
    seconds: float,
    stop_event: threading.Event | None,
    backend: FprintBackend,
) -> bool:
    """Sleep up to ``seconds`` while remaining responsive to ``stop_event``."""
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        backend.pump_events(25)
    return not (stop_event is not None and stop_event.is_set())


def run_verify_rounds(
    backend: FprintBackend,
    *,
    finger: str = "any",
    username: str = "",
    rounds: int = 5,
    gap_seconds: float = 1.0,
    round_timeout_seconds: float = 45.0,
    store: RecordStore | None = None,
    feedback: FeedbackChannel | None = None,
    inhibit_power_key: bool = True,
    lang: str = "en",
    on_event: EventCallback | None = None,
    stop_event: threading.Event | None = None,
) -> VerifyCampaignResult:
    """Run ``rounds`` timed verification rounds with guaranteed sensor release."""
    if rounds < 1:
        raise ValueError("rounds must be >= 1")

    records: list[RoundRecord] = []
    cancelled = False

    with PowerKeyInhibitor(enabled=inhibit_power_key):
        for idx in range(1, rounds + 1):
            if stop_event is not None and stop_event.is_set():
                cancelled = True
                break

            round_started = time.monotonic()
            _emit(
                on_event,
                SessionEvent(
                    state="opening",
                    message=ui_text("opening", lang),
                    round_index=idx,
                    total_rounds=rounds,
                    elapsed_ms=0,
                ),
            )

            claimed = False
            started_verify = False
            sub_id = 0
            state_box: dict[str, object] = {
                "done": False,
                "result": None,
                "retries": [],
            }

            def _on_verify(result: str, done: bool) -> None:
                if done:
                    state_box["result"] = result
                    state_box["done"] = True
                else:
                    retries = state_box["retries"]
                    assert isinstance(retries, list)
                    retries.append(result)

            def _on_enroll(_result: str, _done: bool) -> None:
                pass

            try:
                sub_id = backend.subscribe_signals(_on_verify, _on_enroll)
                backend.claim(username)
                claimed = True
                claim_ms = _ms_since(round_started)

                if stop_event is not None and stop_event.is_set():
                    cancelled = True
                    break

                backend.verify_start(finger)
                started_verify = True
                ready_ms = _ms_since(round_started)
                ready_at = time.monotonic()

                # Contract invariant: ONLY announce "press now" after Claim + VerifyStart returned.
                ready_msg = ui_text("ready_verify", lang)
                _emit(
                    on_event,
                    SessionEvent(
                        state="ready",
                        message=ready_msg,
                        round_index=idx,
                        total_rounds=rounds,
                        elapsed_ms=ready_ms,
                    ),
                )
                if feedback is not None:
                    title = (
                        f"Round {idx}/{rounds}: Press now"
                        if lang == "en"
                        else f"第 {idx}/{rounds} 轮：请按压指纹"
                    )
                    feedback.emit("ready", title, ready_msg)

                seen_retries = 0
                deadline = ready_at + round_timeout_seconds
                while not state_box["done"]:
                    if stop_event is not None and stop_event.is_set():
                        cancelled = True
                        break
                    if time.monotonic() >= deadline:
                        state_box["result"] = "timeout"
                        state_box["done"] = True
                        break
                    backend.pump_events(25)
                    retries = state_box["retries"]
                    assert isinstance(retries, list)
                    while seen_retries < len(retries):
                        code = retries[seen_retries]
                        seen_retries += 1
                        info = explain_result(code, lang)
                        _emit(
                            on_event,
                            SessionEvent(
                                state="retry",
                                message=info.title,
                                guidance=info.guidance,
                                round_index=idx,
                                total_rounds=rounds,
                                result=code,
                                elapsed_ms=_ms_since(round_started),
                            ),
                        )
                        if feedback is not None:
                            feedback.emit("retry", info.title, info.guidance)

                if cancelled:
                    break

                final_code = str(state_box["result"] or "verify-unknown-error")
                total_ms = _ms_since(round_started)
                capture_ms = max(0, total_ms - ready_ms)
                info = explain_result(final_code, lang)

                record = RoundRecord.now(
                    mode="verify",
                    round_num=idx,
                    finger=finger,
                    result=final_code,
                    claim_ms=claim_ms,
                    ready_ms=ready_ms,
                    capture_ms=capture_ms,
                    total_ms=total_ms,
                )
                if store is not None:
                    store.append(record)
                records.append(record)

                _emit(
                    on_event,
                    SessionEvent(
                        state="verdict",
                        message=info.title,
                        guidance=info.guidance,
                        round_index=idx,
                        total_rounds=rounds,
                        result=final_code,
                        matched=info.matched,
                        elapsed_ms=total_ms,
                        record=record,
                    ),
                )
                if feedback is not None:
                    feedback.emit("ok" if info.matched else "fail", info.title, info.guidance)

            except FprintError as exc:
                _emit(
                    on_event,
                    SessionEvent(
                        state="error",
                        message=exc.message,
                        round_index=idx,
                        total_rounds=rounds,
                        result=exc.code,
                        elapsed_ms=_ms_since(round_started),
                    ),
                )
                return VerifyCampaignResult(
                    records=tuple(records),
                    cancelled=False,
                    device_error=f"{exc.code}: {exc.message}",
                )
            finally:
                if started_verify:
                    try:
                        backend.verify_stop()
                    except FprintError:
                        pass
                if claimed:
                    try:
                        backend.release()
                    except FprintError:
                        pass
                if sub_id:
                    try:
                        backend.unsubscribe_signals(sub_id)
                    except Exception:
                        pass

            if idx < rounds and gap_seconds > 0:
                if not _sleep_interruptible(gap_seconds, stop_event, backend):
                    cancelled = True
                    break

    return VerifyCampaignResult(records=tuple(records), cancelled=cancelled)


def run_enroll(
    backend: FprintBackend,
    *,
    finger: str = "right-index-finger",
    username: str = "",
    timeout_seconds: float = 180.0,
    store: RecordStore | None = None,
    feedback: FeedbackChannel | None = None,
    inhibit_power_key: bool = True,
    lang: str = "en",
    on_event: EventCallback | None = None,
    stop_event: threading.Event | None = None,
) -> EnrollOutcome:
    """Run an enrollment session with dynamic stage count and guaranteed release."""
    started = time.monotonic()
    claimed = False
    started_enroll = False
    sub_id = 0
    stages_passed = 0
    total_stages = 0
    cancelled = False

    with PowerKeyInhibitor(enabled=inhibit_power_key):
        try:
            device = backend.get_default_device(username)
            # Never hardcode 8 or 9: read fprintd's num-enroll-stages property.
            total_stages = max(1, device.num_enroll_stages)

            _emit(
                on_event,
                SessionEvent(
                    state="opening",
                    message=ui_text("opening", lang),
                    stage=0,
                    total_stages=total_stages,
                    elapsed_ms=_ms_since(started),
                ),
            )

            events: list[tuple[str, bool]] = []

            def _on_verify(_result: str, _done: bool) -> None:
                pass

            def _on_enroll(result: str, done: bool) -> None:
                events.append((result, done))

            sub_id = backend.subscribe_signals(_on_verify, _on_enroll)
            backend.claim(username)
            claimed = True
            claim_ms = _ms_since(started)

            if stop_event is not None and stop_event.is_set():
                return EnrollOutcome(
                    completed=False,
                    result="cancelled",
                    stages_passed=0,
                    total_stages=total_stages,
                    cancelled=True,
                )

            backend.enroll_start(finger)
            started_enroll = True
            ready_ms = _ms_since(started)
            ready_msg = ui_text(
                "ready_enroll", lang, next_stage=1, total_stages=total_stages
            )
            _emit(
                on_event,
                SessionEvent(
                    state="ready",
                    message=ready_msg,
                    stage=0,
                    total_stages=total_stages,
                    elapsed_ms=ready_ms,
                ),
            )
            if feedback is not None:
                title = (
                    "Enrollment ready" if lang == "en" else "指纹录入已就绪"
                )
                feedback.emit("ready", title, ready_msg)

            seen = 0
            final_result: str | None = None
            deadline = time.monotonic() + timeout_seconds

            while final_result is None:
                if stop_event is not None and stop_event.is_set():
                    cancelled = True
                    final_result = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    final_result = "timeout"
                    break
                backend.pump_events(25)
                while seen < len(events):
                    code, done = events[seen]
                    seen += 1
                    info = explain_result(code, lang)
                    if code == "enroll-stage-passed":
                        stages_passed += 1
                        msg = ui_text(
                            "stage_progress",
                            lang,
                            stage=stages_passed,
                            total_stages=total_stages,
                        )
                        _emit(
                            on_event,
                            SessionEvent(
                                state="stage",
                                message=msg,
                                guidance=info.guidance,
                                stage=stages_passed,
                                total_stages=total_stages,
                                result=code,
                                elapsed_ms=_ms_since(started),
                            ),
                        )
                        if feedback is not None:
                            feedback.emit("stage", f"{stages_passed}/{total_stages}", msg)
                    elif done:
                        if code == "enroll-completed":
                            stages_passed = max(stages_passed, total_stages)
                        final_result = code
                        break
                    else:
                        _emit(
                            on_event,
                            SessionEvent(
                                state="retry",
                                message=info.title,
                                guidance=info.guidance,
                                stage=stages_passed,
                                total_stages=total_stages,
                                result=code,
                                elapsed_ms=_ms_since(started),
                            ),
                        )
                        if feedback is not None:
                            feedback.emit("retry", info.title, info.guidance)

            total_ms = _ms_since(started)
            completed = final_result == "enroll-completed"
            record: RoundRecord | None = None
            if not cancelled:
                record = RoundRecord.now(
                    mode="enroll",
                    round_num=1,
                    finger=finger,
                    result=final_result,
                    claim_ms=claim_ms,
                    ready_ms=ready_ms,
                    capture_ms=max(0, total_ms - ready_ms),
                    total_ms=total_ms,
                )
                if store is not None:
                    store.append(record)

            info = explain_result(final_result, lang)
            _emit(
                on_event,
                SessionEvent(
                    state="verdict",
                    message=info.title,
                    guidance=info.guidance,
                    stage=stages_passed,
                    total_stages=total_stages,
                    result=final_result,
                    matched=completed,
                    elapsed_ms=total_ms,
                    record=record,
                ),
            )
            if feedback is not None and not cancelled:
                feedback.emit("ok" if completed else "fail", info.title, info.guidance)

            return EnrollOutcome(
                completed=completed,
                result=final_result,
                stages_passed=stages_passed,
                total_stages=total_stages,
                record=record,
                cancelled=cancelled,
            )

        except FprintError as exc:
            _emit(
                on_event,
                SessionEvent(
                    state="error",
                    message=exc.message,
                    stage=stages_passed,
                    total_stages=total_stages,
                    result=exc.code,
                    elapsed_ms=_ms_since(started),
                ),
            )
            return EnrollOutcome(
                completed=False,
                result=exc.code,
                stages_passed=stages_passed,
                total_stages=total_stages,
                device_error=f"{exc.code}: {exc.message}",
            )
        finally:
            if started_enroll:
                try:
                    backend.enroll_stop()
                except FprintError:
                    pass
            if claimed:
                try:
                    backend.release()
                except FprintError:
                    pass
            if sub_id:
                try:
                    backend.unsubscribe_signals(sub_id)
                except Exception:
                    pass
