# SPDX-License-Identifier: LGPL-2.1-or-later
"""Single-window, three-page GTK4 + libadwaita UI backed by toolkit.core."""

from __future__ import annotations

import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core import (  # noqa: E402
    DBusFprintBackend,
    DeviceInfo,
    ENROLLABLE_FINGERS,
    FeedbackChannel,
    FprintBackend,
    FprintError,
    RecordStore,
    SessionEvent,
    VALID_FINGERS,
    inspect_powerdevil_config,
    run_doctor,
    run_enroll,
    run_verify_rounds,
    ui_text,
)
from .ring import ProgressRing  # noqa: E402


class ToolkitWindow(Adw.ApplicationWindow):
    """Single window: Status, Enroll, and Timed Verification Test."""

    def __init__(
        self,
        application: Adw.Application,
        *,
        backend_factory: Callable[[], FprintBackend] | None = None,
        state_file: str | None = None,
        lang: str = "en",
        sound: bool = True,
        notify: bool = True,
        inhibit_power_key: bool = True,
    ) -> None:
        super().__init__(application=application)
        self._backend_factory = backend_factory or DBusFprintBackend
        self._store = RecordStore(state_file)
        self._lang = lang
        self._sound_enabled = sound
        self._notify_enabled = notify
        self._inhibit_power_key = inhibit_power_key

        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._device_info: DeviceInfo | None = None

        self.set_title(ui_text("app_title", self._lang))
        self.set_default_size(500, 620)
        self.connect("close-request", self._on_close_request)

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        self._stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        toolbar_view.add_top_bar(header)

        toolbar_view.set_content(self._stack)

        self._build_status_page()
        self._build_enroll_page()
        self._build_verify_page()

        self.refresh_status()

    # ------------------------------------------------------------------ Pages
    def _build_status_page(self) -> None:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(560)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(16)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(box)
        scroll.set_child(clamp)

        # Optional GPD power-button advisory banner
        self._power_banner = Adw.Banner()
        self._power_banner.set_title(ui_text("power_warn_title", self._lang))
        self._power_banner.set_revealed(False)
        box.append(self._power_banner)

        # Device group
        self._device_group = Adw.PreferencesGroup()
        self._device_group.set_title("Sensor" if self._lang == "en" else "指纹传感器")
        box.append(self._device_group)

        self._row_name = Adw.ActionRow(title="Reader" if self._lang == "en" else "设备名称")
        self._row_scan = Adw.ActionRow(title="Scan type" if self._lang == "en" else "扫描类型")
        self._row_stages = Adw.ActionRow(title="Enroll stages" if self._lang == "en" else "录入阶段数")
        self._row_fingers = Adw.ActionRow(title="Enrolled prints" if self._lang == "en" else "已录入指纹")
        for row in (self._row_name, self._row_scan, self._row_stages, self._row_fingers):
            self._device_group.add(row)

        # Quick action buttons
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        actions.set_halign(Gtk.Align.CENTER)

        btn_test = Gtk.Button(label=ui_text("btn_test", self._lang))
        btn_test.add_css_class("suggested-action")
        btn_test.add_css_class("pill")
        btn_test.connect("clicked", lambda _b: self._stack.set_visible_child_name("verify"))

        btn_refresh = Gtk.Button(label=ui_text("btn_refresh", self._lang))
        btn_refresh.add_css_class("pill")
        btn_refresh.connect("clicked", lambda _b: self.refresh_status())

        actions.append(btn_test)
        actions.append(btn_refresh)
        box.append(actions)

        # Doctor diagnostics group
        self._doctor_group = Adw.PreferencesGroup()
        self._doctor_group.set_title("Diagnostics" if self._lang == "en" else "环境体检")
        self._doctor_rows: list[Gtk.Widget] = []
        box.append(self._doctor_group)

        page = self._stack.add_titled(scroll, "status", ui_text("tab_status", self._lang))
        page.set_icon_name("security-high-symbolic")

    def _build_enroll_page(self) -> None:
        clamp = Adw.Clamp()
        clamp.set_maximum_size(480)
        clamp.set_margin_top(20)
        clamp.set_margin_bottom(20)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_valign(Gtk.Align.CENTER)
        clamp.set_child(box)

        # Finger selector
        finger_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        finger_row.set_halign(Gtk.Align.CENTER)
        finger_lbl = Gtk.Label(label="Finger:" if self._lang == "en" else "选择手指：")
        self._enroll_finger = Gtk.DropDown.new_from_strings(list(ENROLLABLE_FINGERS))
        self._enroll_finger.set_selected(ENROLLABLE_FINGERS.index("right-index-finger"))
        finger_row.append(finger_lbl)
        finger_row.append(self._enroll_finger)
        box.append(finger_row)

        self._ring = ProgressRing(total_stages=8)
        self._ring.set_halign(Gtk.Align.CENTER)
        box.append(self._ring)

        self._enroll_spinner = Gtk.Spinner()
        self._enroll_spinner.set_halign(Gtk.Align.CENTER)
        box.append(self._enroll_spinner)

        self._enroll_title = Gtk.Label(
            label="Ready to enroll" if self._lang == "en" else "准备录入指纹"
        )
        self._enroll_title.add_css_class("title-2")
        self._enroll_title.set_wrap(True)
        self._enroll_title.set_justify(Gtk.Justification.CENTER)
        box.append(self._enroll_title)

        self._enroll_guidance = Gtk.Label(label="")
        self._enroll_guidance.add_css_class("dim-label")
        self._enroll_guidance.set_wrap(True)
        self._enroll_guidance.set_justify(Gtk.Justification.CENTER)
        box.append(self._enroll_guidance)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_row.set_halign(Gtk.Align.CENTER)
        self._btn_enroll_start = Gtk.Button(label=ui_text("btn_enroll", self._lang))
        self._btn_enroll_start.add_css_class("suggested-action")
        self._btn_enroll_start.add_css_class("pill")
        self._btn_enroll_start.connect("clicked", self._on_start_enroll)

        self._btn_enroll_stop = Gtk.Button(label=ui_text("btn_stop", self._lang))
        self._btn_enroll_stop.add_css_class("destructive-action")
        self._btn_enroll_stop.add_css_class("pill")
        self._btn_enroll_stop.set_sensitive(False)
        self._btn_enroll_stop.connect("clicked", self._on_stop_clicked)

        btn_row.append(self._btn_enroll_start)
        btn_row.append(self._btn_enroll_stop)
        box.append(btn_row)

        page = self._stack.add_titled(clamp, "enroll", ui_text("tab_enroll", self._lang))
        page.set_icon_name("list-add-symbolic")

    def _build_verify_page(self) -> None:
        clamp = Adw.Clamp()
        clamp.set_maximum_size(480)
        clamp.set_margin_top(20)
        clamp.set_margin_bottom(20)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        clamp.set_child(box)

        # Controls group
        controls = Adw.PreferencesGroup()
        finger_strings = ["any", *ENROLLABLE_FINGERS]
        self._verify_finger_list = finger_strings
        self._verify_finger = Gtk.DropDown.new_from_strings(finger_strings)
        self._verify_finger.set_selected(0)
        row_finger = Adw.ActionRow(title="Finger" if self._lang == "en" else "验证指位")
        row_finger.add_suffix(self._verify_finger)
        controls.add(row_finger)

        adj = Gtk.Adjustment(value=5, lower=1, upper=50, step_increment=1, page_increment=5)
        self._verify_rounds = Gtk.SpinButton(adjustment=adj, digits=0)
        row_rounds = Adw.ActionRow(title="Rounds" if self._lang == "en" else "测试轮次")
        row_rounds.add_suffix(self._verify_rounds)
        controls.add(row_rounds)

        self._switch_sound = Gtk.Switch(active=self._sound_enabled, valign=Gtk.Align.CENTER)
        self._switch_sound.connect("notify::active", lambda sw, _p: setattr(self, "_sound_enabled", sw.get_active()))
        row_sound = Adw.ActionRow(title="Audio cues" if self._lang == "en" else "声音提示")
        row_sound.add_suffix(self._switch_sound)
        controls.add(row_sound)

        self._switch_notify = Gtk.Switch(active=self._notify_enabled, valign=Gtk.Align.CENTER)
        self._switch_notify.connect("notify::active", lambda sw, _p: setattr(self, "_notify_enabled", sw.get_active()))
        row_notify = Adw.ActionRow(title="Desktop notifications" if self._lang == "en" else "桌面通知")
        row_notify.add_suffix(self._switch_notify)
        controls.add(row_notify)
        box.append(controls)

        # Live status card
        self._verify_spinner = Gtk.Spinner()
        self._verify_spinner.set_halign(Gtk.Align.CENTER)
        box.append(self._verify_spinner)

        self._verify_state_label = Gtk.Label(
            label="Idle — click Start Test" if self._lang == "en" else "空闲 — 点击「开始测试」"
        )
        self._verify_state_label.add_css_class("title-2")
        self._verify_state_label.set_wrap(True)
        self._verify_state_label.set_justify(Gtk.Justification.CENTER)
        box.append(self._verify_state_label)

        self._verify_guidance = Gtk.Label(label="")
        self._verify_guidance.add_css_class("dim-label")
        self._verify_guidance.set_wrap(True)
        self._verify_guidance.set_justify(Gtk.Justification.CENTER)
        box.append(self._verify_guidance)

        self._verify_counter = Gtk.Label(label="")
        self._verify_counter.add_css_class("title-4")
        box.append(self._verify_counter)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_row.set_halign(Gtk.Align.CENTER)
        self._btn_verify_start = Gtk.Button(label=ui_text("btn_test", self._lang))
        self._btn_verify_start.add_css_class("suggested-action")
        self._btn_verify_start.add_css_class("pill")
        self._btn_verify_start.connect("clicked", self._on_start_verify)

        self._btn_verify_stop = Gtk.Button(label=ui_text("btn_stop", self._lang))
        self._btn_verify_stop.add_css_class("destructive-action")
        self._btn_verify_stop.add_css_class("pill")
        self._btn_verify_stop.set_sensitive(False)
        self._btn_verify_stop.connect("clicked", self._on_stop_clicked)

        btn_row.append(self._btn_verify_start)
        btn_row.append(self._btn_verify_stop)
        box.append(btn_row)

        page = self._stack.add_titled(clamp, "verify", ui_text("tab_verify", self._lang))
        page.set_icon_name("system-search-symbolic")

    # ------------------------------------------------------------------ Actions
    def _set_running(self, running: bool) -> None:
        self._btn_enroll_start.set_sensitive(not running)
        self._btn_verify_start.set_sensitive(not running)
        self._btn_enroll_stop.set_sensitive(running)
        self._btn_verify_stop.set_sensitive(running)

    def refresh_status(self) -> None:
        pd = inspect_powerdevil_config()
        self._power_banner.set_revealed(pd.may_trigger_power_menu)

        backend: FprintBackend | None = None
        try:
            backend = self._backend_factory()
            info = backend.get_default_device()
            self._device_info = info
            self._row_name.set_subtitle(info.name)
            self._row_scan.set_subtitle(info.scan_type)
            self._row_stages.set_subtitle(str(info.num_enroll_stages))
            self._row_fingers.set_subtitle(
                ", ".join(info.enrolled_fingers) if info.enrolled_fingers else "(none)"
            )
            self._ring.set_progress(0, total=max(1, info.num_enroll_stages), state="idle")
        except FprintError as exc:
            self._device_info = None
            self._row_name.set_subtitle(f"Unavailable ({exc.code})")
            self._row_scan.set_subtitle("-")
            self._row_stages.set_subtitle("-")
            self._row_fingers.set_subtitle(exc.message)

        for old in self._doctor_rows:
            self._doctor_group.remove(old)
        self._doctor_rows.clear()
        for check in run_doctor(backend=backend):
            row = Adw.ActionRow(title=f"[{check.status.upper()}] {check.name}")
            row.set_subtitle(check.detail)
            self._doctor_group.add(row)
            self._doctor_rows.append(row)

    def _on_stop_clicked(self, _btn) -> None:
        self._stop_event.set()

    def _on_close_request(self, _win) -> bool:
        self._stop_event.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2.0)
        return False

    def _on_start_enroll(self, _btn) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        finger = ENROLLABLE_FINGERS[self._enroll_finger.get_selected()]
        self._stop_event.clear()
        self._set_running(True)

        def _worker():
            try:
                backend = self._backend_factory()
                feedback = FeedbackChannel(
                    sound=self._sound_enabled, notify=self._notify_enabled
                )
                run_enroll(
                    backend,
                    finger=finger,
                    store=self._store,
                    feedback=feedback,
                    inhibit_power_key=self._inhibit_power_key,
                    lang=self._lang,
                    on_event=lambda ev: GLib.idle_add(self._handle_enroll_event, ev),
                    stop_event=self._stop_event,
                )
            except FprintError as exc:
                GLib.idle_add(
                    self._handle_enroll_event,
                    SessionEvent(state="error", message=exc.message, result=exc.code),
                )
            finally:
                GLib.idle_add(self._finish_worker)

        self._worker = threading.Thread(target=_worker, daemon=True)
        self._worker.start()

    def _handle_enroll_event(self, ev: SessionEvent) -> bool:
        if ev.state == "opening":
            self._enroll_spinner.start()
            self._ring.set_progress(ev.stage, ev.total_stages or None, state="opening")
        elif ev.state == "ready":
            self._enroll_spinner.stop()
            self._ring.set_progress(ev.stage, ev.total_stages or None, state="ready")
            self.present()
        elif ev.state == "stage":
            self._ring.set_progress(ev.stage, ev.total_stages or None, state="ready")
        elif ev.state == "retry":
            self._ring.set_progress(ev.stage, ev.total_stages or None, state="retry")
        elif ev.state == "verdict":
            self._enroll_spinner.stop()
            self._ring.set_progress(
                ev.stage, ev.total_stages or None, state="ok" if ev.matched else "error"
            )
        else:
            self._enroll_spinner.stop()
            self._ring.set_progress(ev.stage, ev.total_stages or None, state="error")

        self._enroll_title.set_label(ev.message)
        self._enroll_guidance.set_label(ev.guidance)
        return False

    def _on_start_verify(self, _btn) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        finger = self._verify_finger_list[self._verify_finger.get_selected()]
        if finger not in VALID_FINGERS:
            finger = "any"
        rounds = int(self._verify_rounds.get_value())
        self._stop_event.clear()
        self._set_running(True)
        self._verify_counter.set_label(f"0 / {rounds}")

        def _worker():
            matches = 0
            completed = 0

            def _dispatch(ev: SessionEvent) -> None:
                nonlocal matches, completed
                if ev.state == "verdict":
                    completed += 1
                    if ev.matched:
                        matches += 1
                GLib.idle_add(self._handle_verify_event, ev, matches, completed)

            try:
                backend = self._backend_factory()
                feedback = FeedbackChannel(
                    sound=self._sound_enabled, notify=self._notify_enabled
                )
                result = run_verify_rounds(
                    backend,
                    finger=finger,
                    rounds=rounds,
                    store=self._store,
                    feedback=feedback,
                    inhibit_power_key=self._inhibit_power_key,
                    lang=self._lang,
                    on_event=_dispatch,
                    stop_event=self._stop_event,
                )
                GLib.idle_add(self._show_verify_summary, result.matches, result.total)
            except FprintError as exc:
                GLib.idle_add(
                    self._handle_verify_event,
                    SessionEvent(state="error", message=exc.message, result=exc.code),
                    matches,
                    completed,
                )
            finally:
                GLib.idle_add(self._finish_worker)

        self._worker = threading.Thread(target=_worker, daemon=True)
        self._worker.start()

    def _handle_verify_event(self, ev: SessionEvent, matches: int, completed: int) -> bool:
        if ev.state == "opening":
            self._verify_spinner.start()
        elif ev.state == "ready":
            self._verify_spinner.stop()
            self.present()
        elif ev.state in ("verdict", "error"):
            self._verify_spinner.stop()
        prefix = f"Round {ev.round_index}/{ev.total_rounds}: " if ev.total_rounds else ""
        self._verify_state_label.set_label(f"{prefix}{ev.message}")
        self._verify_guidance.set_label(ev.guidance)
        if ev.total_rounds:
            self._verify_counter.set_label(f"{matches} / {completed} (target {ev.total_rounds})")
        return False

    def _show_verify_summary(self, matches: int, total: int) -> bool:
        if total > 0:
            pct = 100.0 * matches / total
            self._verify_counter.set_label(f"Result: {matches}/{total} ({pct:.1f}%)")
        return False

    def _finish_worker(self) -> bool:
        self._set_running(False)
        self.refresh_status()
        return False
