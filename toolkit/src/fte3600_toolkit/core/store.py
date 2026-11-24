# SPDX-License-Identifier: LGPL-2.1-or-later
"""Append-only JSONL round telemetry store and report aggregator.

Stores only timings, finger names, and fprintd status strings.
Never stores fingerprint images, templates, or credentials.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
from statistics import median
from typing import Iterable

SCHEMA_VERSION = 1


def default_state_path() -> Path:
    """Resolve the default rounds.jsonl path under XDG_STATE_HOME."""
    override = os.environ.get("FTE3600_TOOLKIT_STATE_FILE")
    if override:
        return Path(override)
    state_dir = os.environ.get("FTE3600_TOOLKIT_STATE_DIR")
    if state_dir:
        return Path(state_dir) / "rounds.jsonl"
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return base / "fte3600-toolkit" / "rounds.jsonl"


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """One recorded verify or enroll round (schema v1)."""

    ts: str
    mode: str  # "verify" | "enroll"
    round: int
    finger: str
    result: str
    claim_ms: int
    ready_ms: int
    total_ms: int
    capture_ms: int | None = None
    schema: int = SCHEMA_VERSION

    @classmethod
    def now(
        cls,
        *,
        mode: str,
        round_num: int,
        finger: str,
        result: str,
        claim_ms: int,
        ready_ms: int,
        total_ms: int,
        capture_ms: int | None = None,
    ) -> "RoundRecord":
        ts = datetime.now().astimezone().isoformat(timespec="milliseconds")
        return cls(
            schema=SCHEMA_VERSION,
            ts=ts,
            mode=mode,
            round=round_num,
            finger=finger,
            result=result,
            claim_ms=max(0, int(claim_ms)),
            ready_ms=max(0, int(ready_ms)),
            total_ms=max(0, int(total_ms)),
            capture_ms=max(0, int(capture_ms)) if capture_ms is not None else None,
        )

    def to_json_line(self) -> str:
        payload = asdict(self)
        if payload["capture_ms"] is None:
            del payload["capture_ms"]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict) -> "RoundRecord":
        if not isinstance(data, dict):
            raise ValueError("record must be a JSON object")
        required = ("ts", "mode", "round", "finger", "result", "claim_ms", "ready_ms", "total_ms")
        for key in required:
            if key not in data:
                raise ValueError(f"missing required field: {key}")
        capture_raw = data.get("capture_ms")
        return cls(
            schema=int(data.get("schema", SCHEMA_VERSION)),
            ts=str(data["ts"]),
            mode=str(data["mode"]),
            round=int(data["round"]),
            finger=str(data["finger"]),
            result=str(data["result"]),
            claim_ms=int(data["claim_ms"]),
            ready_ms=int(data["ready_ms"]),
            total_ms=int(data["total_ms"]),
            capture_ms=int(capture_raw) if capture_raw is not None else None,
        )


@dataclass(frozen=True, slots=True)
class ReportSummary:
    """Aggregated recognition metrics across stored rounds."""

    total: int
    matches: int
    match_rate: float
    median_claim_ms: float | None
    median_ready_ms: float | None
    median_total_ms: float | None
    p95_total_ms: float | None
    histogram: dict[str, int]
    malformed_lines: int = 0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "matches": self.matches,
            "match_rate": round(self.match_rate, 6),
            "median_claim_ms": self.median_claim_ms,
            "median_ready_ms": self.median_ready_ms,
            "median_total_ms": self.median_total_ms,
            "p95_total_ms": self.p95_total_ms,
            "histogram": dict(self.histogram),
            "malformed_lines": self.malformed_lines,
        }


def _percentile(values: list[int], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int((pct / 100.0) * (len(ordered) - 1) + 0.5)))
    return float(ordered[idx])


def summarize_records(
    records: Iterable[RoundRecord],
    *,
    mode: str | None = "verify",
    finger: str | None = None,
    malformed_lines: int = 0,
) -> ReportSummary:
    """Compute match rate, latency medians/p95, and failure histogram."""
    selected = [
        r
        for r in records
        if (mode is None or r.mode == mode) and (finger is None or r.finger == finger)
    ]
    total = len(selected)
    matches = sum(
        1 for r in selected if r.result in ("verify-match", "enroll-completed")
    )
    match_rate = (matches / total) if total > 0 else 0.0
    histogram = dict(sorted(Counter(r.result for r in selected).items()))
    claim_vals = [r.claim_ms for r in selected]
    ready_vals = [r.ready_ms for r in selected]
    total_vals = [r.total_ms for r in selected]
    return ReportSummary(
        total=total,
        matches=matches,
        match_rate=match_rate,
        median_claim_ms=float(median(claim_vals)) if claim_vals else None,
        median_ready_ms=float(median(ready_vals)) if ready_vals else None,
        median_total_ms=float(median(total_vals)) if total_vals else None,
        p95_total_ms=_percentile(total_vals, 95.0),
        histogram=histogram,
        malformed_lines=malformed_lines,
    )


class RecordStore:
    """Append-only JSONL store on local disk."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_state_path()

    def append(self, record: RoundRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = record.to_json_line() + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def load(self) -> tuple[list[RoundRecord], int]:
        if not self.path.exists():
            return [], 0
        records: list[RoundRecord] = []
        malformed = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    records.append(RoundRecord.from_dict(data))
                except (json.JSONDecodeError, ValueError, TypeError):
                    malformed += 1
        return records, malformed

    def summarize(
        self,
        *,
        mode: str | None = "verify",
        finger: str | None = None,
    ) -> ReportSummary:
        records, malformed = self.load()
        return summarize_records(
            records, mode=mode, finger=finger, malformed_lines=malformed
        )
