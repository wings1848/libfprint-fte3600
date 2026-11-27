# SPDX-License-Identifier: LGPL-2.1-or-later
"""Unified fprintd status vocabulary and localized user guidance."""

from __future__ import annotations

from dataclasses import dataclass
import os

VALID_FINGERS: tuple[str, ...] = (
    "left-thumb",
    "left-index-finger",
    "left-middle-finger",
    "left-ring-finger",
    "left-little-finger",
    "right-thumb",
    "right-index-finger",
    "right-middle-finger",
    "right-ring-finger",
    "right-little-finger",
    "any",
)

ENROLLABLE_FINGERS: tuple[str, ...] = tuple(f for f in VALID_FINGERS if f != "any")


@dataclass(frozen=True, slots=True)
class ResultExplanation:
    """Plain-language explanation for one fprintd status string."""

    code: str
    kind: str  # "ok", "stage", "retry", "no-match", "error"
    title: str
    guidance: str
    matched: bool = False


_MESSAGES: dict[str, dict[str, tuple[str, str, str, bool]]] = {
    "en": {
        "verify-match": (
            "ok",
            "Recognised",
            "Fingerprint matched.",
            True,
        ),
        "verify-no-match": (
            "no-match",
            "Not recognised",
            "Press firmly, cover the whole pad, and hold for ~1 s.",
            False,
        ),
        "verify-retry-scan": (
            "retry",
            "Image unclear, try again",
            "Lift your finger completely and press again.",
            False,
        ),
        "verify-finger-not-centered": (
            "retry",
            "Finger not centred",
            "Move your finger to the centre of the sensor pad.",
            False,
        ),
        "verify-remove-and-retry": (
            "retry",
            "Lift and press again",
            "Remove your finger fully before pressing again.",
            False,
        ),
        "verify-swipe-too-short": (
            "retry",
            "Contact too brief",
            "Hold your finger on the sensor for ~1 s.",
            False,
        ),
        "verify-disconnected": (
            "error",
            "Sensor disconnected",
            "Stop and check `fte3600-toolkit doctor` and `journalctl -u fprintd -b`.",
            False,
        ),
        "verify-unknown-error": (
            "error",
            "No finger detected or sensor error",
            "If this appeared after ~30 s, no press reached the sensor; press only after the ready prompt.",
            False,
        ),
        "enroll-stage-passed": (
            "stage",
            "Sample accepted",
            "Lift completely, keep the same finger angle, and shift only 1-2 mm (~70% overlap) for the next press.",
            False,
        ),
        "enroll-completed": (
            "ok",
            "Enrollment finished",
            "All samples accepted; your fingerprint is ready to verify.",
            True,
        ),
        "enroll-retry-scan": (
            "retry",
            "Sample rejected, try again",
            "Lift fully, keep the same finger angle, re-centre (~70% overlap), and press firmly.",
            False,
        ),
        "enroll-remove-and-retry": (
            "retry",
            "Duplicate or still touching",
            "Lift completely, keep the same finger angle, and shift 1-2 mm (~70% overlap) before pressing again.",
            False,
        ),
        "enroll-finger-not-centered": (
            "retry",
            "Finger not centred",
            "Place your finger firmly over the middle of the pad.",
            False,
        ),
        "enroll-swipe-too-short": (
            "retry",
            "Contact too brief",
            "Press firmly and hold for ~1 s.",
            False,
        ),
        "enroll-data-full": (
            "error",
            "Storage full",
            "Delete an old print first (`fte3600-toolkit delete`).",
            False,
        ),
        "enroll-disconnected": (
            "error",
            "Sensor disconnected",
            "Stop and check `fte3600-toolkit doctor` and `journalctl -u fprintd -b`.",
            False,
        ),
        "enroll-failed": (
            "error",
            "Enrollment failed",
            "Retry enrollment with firm, full-pad presses.",
            False,
        ),
        "enroll-unknown-error": (
            "error",
            "Enrollment interrupted or timed out",
            "Keep the sensor uncovered while waking, then press after the ready prompt.",
            False,
        ),
        "timeout": (
            "error",
            "Timed out waiting for sensor",
            "No response arrived from fprintd before the client deadline.",
            False,
        ),
        "cancelled": (
            "error",
            "Cancelled",
            "Operation stopped and sensor released.",
            False,
        ),
    },
    "zh": {
        "verify-match": (
            "ok",
            "识别成功",
            "指纹匹配通过。",
            True,
        ),
        "verify-no-match": (
            "no-match",
            "未识别",
            "请按实、盖满传感器并保持约 1 秒后抬起。",
            False,
        ),
        "verify-retry-scan": (
            "retry",
            "图像不清晰，请重试",
            "请完全抬起手指后再按一次。",
            False,
        ),
        "verify-finger-not-centered": (
            "retry",
            "手指未居中",
            "请将指腹移至传感器中央后按实。",
            False,
        ),
        "verify-remove-and-retry": (
            "retry",
            "请抬起后重按",
            "请先完全离开传感器，再重新按下。",
            False,
        ),
        "verify-swipe-too-short": (
            "retry",
            "按压时间过短",
            "请按实并停留约 1 秒。",
            False,
        ),
        "verify-disconnected": (
            "error",
            "传感器断开连接",
            "请运行 `fte3600-toolkit doctor` 并查看 `journalctl -u fprintd -b`。",
            False,
        ),
        "verify-unknown-error": (
            "error",
            "未检测到手指或传感器异常",
            "若在约 30 秒后出现，说明采集窗口内未收到按压；请在出现就绪提示后再按。",
            False,
        ),
        "enroll-stage-passed": (
            "stage",
            "样本已通过",
            "请完全抬起手指，保持相同角度，仅平移 1-2 毫米（保留约 70% 重叠）后再次按实。",
            False,
        ),
        "enroll-completed": (
            "ok",
            "录入完成",
            "全部样本已通过，现在可以测试识别。",
            True,
        ),
        "enroll-retry-scan": (
            "retry",
            "样本未通过，请再按一次",
            "请完全抬起手指，保持相同角度并居中覆盖传感器（保留约 70% 重叠），再按实重试。",
            False,
        ),
        "enroll-remove-and-retry": (
            "retry",
            "重复样本或未抬起手指",
            "请完全抬起手指，保持相同角度并平移 1-2 毫米（保留约 70% 重叠）后再按。",
            False,
        ),
        "enroll-finger-not-centered": (
            "retry",
            "手指未居中",
            "请将指腹完整覆盖传感器中央。",
            False,
        ),
        "enroll-swipe-too-short": (
            "retry",
            "按压时间过短",
            "请按实并保持约 1 秒。",
            False,
        ),
        "enroll-data-full": (
            "error",
            "指纹存储已满",
            "请先删除旧指纹（`fte3600-toolkit delete`）。",
            False,
        ),
        "enroll-disconnected": (
            "error",
            "传感器断开连接",
            "请运行 `fte3600-toolkit doctor` 并检查驱动状态。",
            False,
        ),
        "enroll-failed": (
            "error",
            "录入失败",
            "请保持按实、盖满传感器后重新录入。",
            False,
        ),
        "enroll-unknown-error": (
            "error",
            "录入超时或中断",
            "唤醒期间请勿触碰传感器，待提示就绪后再按。",
            False,
        ),
        "timeout": (
            "error",
            "等待传感器响应超时",
            "在客户端超时前未收到 fprintd 信号。",
            False,
        ),
        "cancelled": (
            "error",
            "已取消",
            "已停止操作并释放传感器。",
            False,
        ),
    },
}

UI_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "app_title": "FTE3600 Fingerprint Toolkit",
        "opening": "Waking the sensor… do not press yet",
        "ready_verify": "Press now — cover the whole pad and hold ~1 s",
        "ready_enroll": "Press now to capture sample {next_stage} of {total_stages}",
        "capturing": "Reading… keep still",
        "early_press_hint": "Sensor was still waking up — wait for the prompt and press again",
        "stage_progress": "Sample {stage} of {total_stages} accepted — lift, keep angle, shift 1-2 mm (~70% overlap)",
        "tab_status": "Status",
        "tab_enroll": "Enroll",
        "tab_verify": "Test",
        "btn_test": "Start Test",
        "btn_stop": "Stop",
        "btn_enroll": "Start Enrollment",
        "btn_refresh": "Refresh",
        "power_warn_title": "Power button shares the fingerprint key",
        "power_warn_body": "KDE PowerDevil is configured to act on short power-button presses. Set Power Button Action to 'Do nothing' in System Settings if firm presses open the shutdown dialog.",
    },
    "zh": {
        "app_title": "FTE3600 指纹工具箱",
        "opening": "正在唤醒传感器… 请勿提前按压",
        "ready_verify": "请现在按压 — 盖满传感器并按实约 1 秒",
        "ready_enroll": "请按压传感器录入第 {next_stage}/{total_stages} 个样本",
        "capturing": "正在读取… 请保持不动",
        "early_press_hint": "传感器仍在唤醒中 — 请等待就绪提示后再按",
        "stage_progress": "已通过第 {stage}/{total_stages} 个样本 — 请抬起，保持角度平移 1-2 毫米（约 70% 重叠）",
        "tab_status": "状态",
        "tab_enroll": "录入",
        "tab_verify": "识别测试",
        "btn_test": "开始测试",
        "btn_stop": "停止",
        "btn_enroll": "开始录入",
        "btn_refresh": "刷新",
        "power_warn_title": "电源键与指纹键复用提示",
        "power_warn_body": "检测到 KDE PowerDevil 配置了电源键短按动作。若按实指纹弹出关机菜单，请在「系统设置 → 电源管理」将电源键短按设为「无操作」。",
    },
}


def resolve_language(lang: str | None = None) -> str:
    """Resolve explicit language or system locale to 'en' or 'zh'."""
    if lang and lang.lower() in _MESSAGES:
        return lang.lower()
    if lang and lang.lower() != "auto":
        return "en"
    env_lang = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    if env_lang.lower().startswith("zh"):
        return "zh"
    return "en"


def ui_text(key: str, lang: str = "en", **kwargs: object) -> str:
    table = UI_STRINGS.get(lang, UI_STRINGS["en"])
    template = table.get(key, UI_STRINGS["en"].get(key, key))
    return template.format(**kwargs) if kwargs else template


def explain_result(code: str, lang: str = "en") -> ResultExplanation:
    """Return a structured plain-language explanation for a fprintd status."""
    table = _MESSAGES.get(lang, _MESSAGES["en"])
    entry = table.get(code)
    if entry is None:
        fallback_guidance = (
            "Unexpected status from fprintd."
            if lang == "en"
            else "收到来自 fprintd 的未知状态码。"
        )
        return ResultExplanation(
            code=code,
            kind="error",
            title=code,
            guidance=fallback_guidance,
            matched=False,
        )
    kind, title, guidance, matched = entry
    return ResultExplanation(
        code=code,
        kind=kind,
        title=title,
        guidance=guidance,
        matched=matched,
    )
