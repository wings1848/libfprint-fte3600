# SPDX-License-Identifier: LGPL-2.1-or-later
"""Command-line front end for fte3600-toolkit."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import signal
import sys
import threading
from typing import Sequence

from .. import __version__
from ..core import (
    DBusFprintBackend,
    ENROLLABLE_FINGERS,
    FeedbackChannel,
    FprintBackend,
    FprintError,
    RecordStore,
    SessionEvent,
    VALID_FINGERS,
    resolve_language,
    run_doctor,
    run_enroll,
    run_verify_rounds,
)


def _install_signal_handlers(stop_event: threading.Event):
    previous: dict[int, object] = {}

    def _handler(_signum, _frame):
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, _handler)
    return previous


def _restore_signal_handlers(previous: dict[int, object]) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def _add_common_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    d_false = argparse.SUPPRESS if suppress_defaults else False
    d_auto = argparse.SUPPRESS if suppress_defaults else "auto"
    d_none = argparse.SUPPRESS if suppress_defaults else None
    d_empty = argparse.SUPPRESS if suppress_defaults else ""

    parser.add_argument("--json", action="store_true", default=d_false, help="Emit machine-readable JSON output")
    parser.add_argument("--lang", choices=("auto", "en", "zh"), default=d_auto, help="UI language")
    parser.add_argument("--state-file", default=d_none, help="Override path to rounds.jsonl")
    parser.add_argument("--user", default=d_empty, help="Target username (empty = current logged-in user)")
    parser.add_argument("--no-sound", action="store_true", default=d_false, help="Disable audio cues")
    parser.add_argument("--no-notify", action="store_true", default=d_false, help="Disable desktop notifications")
    parser.add_argument(
        "--no-power-inhibit",
        action="store_true",
        default=d_false,
        help="Do not acquire systemd-logind handle-power-key inhibitor lock",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fte3600-toolkit",
        description="Unprivileged enrollment, timed verification, and diagnostics for FTE3600 / GPD fingerprint readers.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_common_options(parser, suppress_defaults=False)

    # Allow global flags both before and after the subcommand without clobbering
    # values already parsed at the top level.
    common_sub = argparse.ArgumentParser(add_help=False)
    _add_common_options(common_sub, suppress_defaults=True)

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", parents=[common_sub], help="Show default fprintd device, scan type, and enrolled fingers")
    sub.add_parser(
        "doctor",
        parents=[common_sub],
        help="Run unprivileged read-only diagnostics for ACPI, glue, spidev, fprintd, and PowerDevil",
    )
    sub.add_parser("list", parents=[common_sub], help="List enrolled fingers for the user")

    p_verify = sub.add_parser("verify", parents=[common_sub], help="Run N timed verification rounds and record telemetry")
    p_verify.add_argument("--rounds", type=int, default=5, help="Number of rounds (default: 5)")
    p_verify.add_argument(
        "--finger",
        default="any",
        choices=VALID_FINGERS,
        help="Finger to verify or 'any' for gallery identification (default: any)",
    )
    p_verify.add_argument("--gap", type=float, default=1.5, help="Pause between rounds in seconds (default: 1.5)")
    p_verify.add_argument("--timeout", type=float, default=45.0, help="Per-round capture timeout in seconds (default: 45)")
    p_verify.add_argument("--no-record", action="store_true", help="Do not append rounds to rounds.jsonl")

    p_enroll = sub.add_parser("enroll", parents=[common_sub], help="Enroll a finger with per-stage progress feedback")
    p_enroll.add_argument(
        "--finger",
        default="right-index-finger",
        choices=ENROLLABLE_FINGERS,
        help="Finger slot to enroll (default: right-index-finger)",
    )
    p_enroll.add_argument("--timeout", type=float, default=180.0, help="Enrollment timeout in seconds (default: 180)")
    p_enroll.add_argument("--no-record", action="store_true", help="Do not append enroll outcome to rounds.jsonl")

    p_delete = sub.add_parser("delete", parents=[common_sub], help="Delete one enrolled finger or all fingers")
    p_delete.add_argument("--finger", choices=ENROLLABLE_FINGERS, help="Specific finger to delete")
    p_delete.add_argument("--all", action="store_true", help="Delete all enrolled fingers for the user")

    p_report = sub.add_parser("report", parents=[common_sub], help="Aggregate match rate, latency medians, and failure breakdown")
    p_report.add_argument("--mode", choices=("verify", "enroll", "all"), default="verify")
    p_report.add_argument("--finger", default=None, help="Filter report to one finger name")

    return parser


def _cmd_status(args: argparse.Namespace, backend: FprintBackend) -> int:
    try:
        info = backend.get_default_device(args.user)
    except FprintError as exc:
        if args.json:
            print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
        else:
            print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(info.to_dict(), ensure_ascii=False))
    else:
        fingers = ", ".join(info.enrolled_fingers) if info.enrolled_fingers else "(none)"
        print(f"Device: {info.name}")
        print(f"Object: {info.object_path}")
        print(f"Scan type: {info.scan_type}")
        print(f"Enroll stages: {info.num_enroll_stages}")
        print(f"Enrolled fingers: {fingers}")
    return 0


def _cmd_list(args: argparse.Namespace, backend: FprintBackend) -> int:
    try:
        fingers = backend.list_enrolled_fingers(args.user)
    except FprintError as exc:
        if args.json:
            print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
        else:
            print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"fingers": fingers}, ensure_ascii=False))
    else:
        for finger in fingers:
            print(finger)
    return 0


def _cmd_doctor(args: argparse.Namespace, backend: FprintBackend | None) -> int:
    checks = run_doctor(backend=backend, username=args.user)
    has_fail = any(c.status == "fail" for c in checks)
    if args.json:
        print(json.dumps({"checks": [c.to_dict() for c in checks]}, ensure_ascii=False))
    else:
        badges = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
        for c in checks:
            print(f"[{badges.get(c.status, c.status)}] {c.name}: {c.detail}")
            if c.hint:
                print(f"       hint: {c.hint}")
    return 1 if has_fail else 0


def _cmd_delete(args: argparse.Namespace, backend: FprintBackend) -> int:
    if not args.all and not args.finger:
        print("error: specify --finger <name> or --all", file=sys.stderr)
        return 2
    try:
        if args.all:
            backend.delete_enrolled_fingers(args.user)
        else:
            backend.delete_enrolled_finger(args.finger, args.user)
    except FprintError as exc:
        if args.json:
            print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
        else:
            print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"deleted": "all" if args.all else args.finger}, ensure_ascii=False))
    else:
        target = "all prints" if args.all else args.finger
        print(f"Deleted: {target}")
    return 0


def _cmd_verify(args: argparse.Namespace, backend: FprintBackend, lang: str) -> int:
    store = None if args.no_record else RecordStore(args.state_file)
    feedback = FeedbackChannel(sound=not args.no_sound, notify=not args.no_notify)
    stop_event = threading.Event()
    previous = _install_signal_handlers(stop_event)

    def _on_event(ev: SessionEvent) -> None:
        if args.json:
            payload = {
                "event": ev.state,
                "round": ev.round_index,
                "total_rounds": ev.total_rounds,
                "elapsed_ms": ev.elapsed_ms,
                "message": ev.message,
                "result": ev.result,
                "matched": ev.matched,
            }
            if ev.record is not None:
                payload["record"] = asdict(ev.record)
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            return
        prefix = f"[{ev.round_index}/{ev.total_rounds} +{ev.elapsed_ms / 1000:5.2f}s]"
        if ev.state == "verdict":
            mark = "PASS" if ev.matched else "MISS"
            print(f"{prefix} {mark} {ev.result}: {ev.message}", flush=True)
            if ev.guidance and not ev.matched:
                print(f"  -> {ev.guidance}", flush=True)
        elif ev.state == "ready":
            print(f"{prefix} READY: {ev.message}", flush=True)
        elif ev.state == "opening":
            print(f"{prefix} OPENING: {ev.message}", flush=True)
        elif ev.state == "retry":
            print(f"{prefix} RETRY ({ev.result}): {ev.message} — {ev.guidance}", flush=True)
        else:
            print(f"{prefix} ERROR ({ev.result}): {ev.message}", file=sys.stderr, flush=True)

    try:
        outcome = run_verify_rounds(
            backend,
            finger=args.finger,
            username=args.user,
            rounds=args.rounds,
            gap_seconds=args.gap,
            round_timeout_seconds=args.timeout,
            store=store,
            feedback=feedback,
            inhibit_power_key=not args.no_power_inhibit,
            lang=lang,
            on_event=_on_event,
            stop_event=stop_event,
        )
    finally:
        _restore_signal_handlers(previous)

    if args.json:
        summary = {
            "summary": True,
            "matches": outcome.matches,
            "total": outcome.total,
            "match_rate": round(outcome.matches / outcome.total, 6) if outcome.total else 0.0,
            "cancelled": outcome.cancelled,
            "device_error": outcome.device_error,
        }
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    elif outcome.total > 0:
        pct = 100.0 * outcome.matches / outcome.total
        print(f"Summary: {outcome.matches}/{outcome.total} matched ({pct:.1f}%)", flush=True)
    return outcome.exit_code


def _cmd_enroll(args: argparse.Namespace, backend: FprintBackend, lang: str) -> int:
    store = None if args.no_record else RecordStore(args.state_file)
    feedback = FeedbackChannel(sound=not args.no_sound, notify=not args.no_notify)
    stop_event = threading.Event()
    previous = _install_signal_handlers(stop_event)

    def _on_event(ev: SessionEvent) -> None:
        if args.json:
            payload = {
                "event": ev.state,
                "stage": ev.stage,
                "total_stages": ev.total_stages,
                "elapsed_ms": ev.elapsed_ms,
                "message": ev.message,
                "result": ev.result,
                "completed": ev.matched,
            }
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            return
        prefix = f"[{ev.stage}/{ev.total_stages} +{ev.elapsed_ms / 1000:5.2f}s]"
        if ev.state == "ready":
            print(f"{prefix} READY: {ev.message}", flush=True)
        elif ev.state == "stage":
            print(f"{prefix} STAGE: {ev.message}", flush=True)
        elif ev.state == "retry":
            print(f"{prefix} RETRY ({ev.result}): {ev.message} — {ev.guidance}", flush=True)
        elif ev.state == "verdict":
            mark = "OK" if ev.matched else "FAIL"
            print(f"{prefix} {mark} ({ev.result}): {ev.message}", flush=True)
        elif ev.state == "opening":
            print(f"{prefix} OPENING: {ev.message}", flush=True)
        else:
            print(f"{prefix} ERROR ({ev.result}): {ev.message}", file=sys.stderr, flush=True)

    try:
        outcome = run_enroll(
            backend,
            finger=args.finger,
            username=args.user,
            timeout_seconds=args.timeout,
            store=store,
            feedback=feedback,
            inhibit_power_key=not args.no_power_inhibit,
            lang=lang,
            on_event=_on_event,
            stop_event=stop_event,
        )
    finally:
        _restore_signal_handlers(previous)

    return outcome.exit_code


def _cmd_report(args: argparse.Namespace) -> int:
    store = RecordStore(args.state_file)
    mode = None if args.mode == "all" else args.mode
    summary = store.summarize(mode=mode, finger=args.finger)
    if args.json:
        print(json.dumps(summary.to_dict(), ensure_ascii=False))
        return 0

    print(f"Store: {store.path}")
    print(f"Rounds: {summary.total} (matches: {summary.matches}, rate: {summary.match_rate * 100:.1f}%)")
    if summary.median_claim_ms is not None:
        print(
            f"Timing (median): claim={summary.median_claim_ms:.0f} ms, "
            f"ready={summary.median_ready_ms:.0f} ms, "
            f"total={summary.median_total_ms:.0f} ms "
            f"(p95={summary.p95_total_ms:.0f} ms)"
        )
    if summary.histogram:
        print("Outcomes:")
        for code, count in summary.histogram.items():
            print(f"  {code}: {count}")
    if summary.malformed_lines:
        print(f"Warning: skipped {summary.malformed_lines} malformed line(s)")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: FprintBackend | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    lang = resolve_language(args.lang)

    if args.command == "report":
        return _cmd_report(args)

    active_backend = backend
    if active_backend is None:
        try:
            active_backend = DBusFprintBackend()
        except FprintError as exc:
            if args.command == "doctor":
                return _cmd_doctor(args, None)
            if args.json:
                print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
            else:
                print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
            return 2

    if args.command == "status":
        return _cmd_status(args, active_backend)
    if args.command == "list":
        return _cmd_list(args, active_backend)
    if args.command == "doctor":
        return _cmd_doctor(args, active_backend)
    if args.command == "delete":
        return _cmd_delete(args, active_backend)
    if args.command == "verify":
        return _cmd_verify(args, active_backend, lang)
    if args.command == "enroll":
        return _cmd_enroll(args, active_backend, lang)
    return 2


def entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
