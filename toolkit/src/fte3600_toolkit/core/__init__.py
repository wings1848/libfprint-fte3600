# SPDX-License-Identifier: LGPL-2.1-or-later
"""Shared core for FTE3600 CLI and GTK4/libadwaita GUI."""

from .backend import DBusFprintBackend, DeviceInfo, FprintBackend, FprintError
from .doctor import CheckResult, run_doctor
from .feedback import FeedbackChannel
from .power import PowerDevilStatus, PowerKeyInhibitor, inspect_powerdevil_config
from .session import EnrollOutcome, SessionEvent, VerifyCampaignResult, run_enroll, run_verify_rounds
from .store import RecordStore, ReportSummary, RoundRecord, default_state_path, summarize_records
from .vocabulary import (
    ENROLLABLE_FINGERS,
    VALID_FINGERS,
    ResultExplanation,
    explain_result,
    resolve_language,
    ui_text,
)

__all__ = [
    "CheckResult",
    "DBusFprintBackend",
    "DeviceInfo",
    "ENROLLABLE_FINGERS",
    "EnrollOutcome",
    "FeedbackChannel",
    "FprintBackend",
    "FprintError",
    "PowerDevilStatus",
    "PowerKeyInhibitor",
    "RecordStore",
    "ReportSummary",
    "ResultExplanation",
    "RoundRecord",
    "SessionEvent",
    "VALID_FINGERS",
    "VerifyCampaignResult",
    "default_state_path",
    "explain_result",
    "inspect_powerdevil_config",
    "resolve_language",
    "run_doctor",
    "run_enroll",
    "run_verify_rounds",
    "summarize_records",
    "ui_text",
]
