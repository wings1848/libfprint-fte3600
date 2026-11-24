# SPDX-License-Identifier: LGPL-2.1-or-later
"""Dynamic N-segment Cairo progress ring for GTK4 enrollment UI."""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


class ProgressRing(Gtk.DrawingArea):
    """Segmented circular indicator sized dynamically from num-enroll-stages."""

    def __init__(self, total_stages: int = 8) -> None:
        super().__init__()
        self._total = max(1, int(total_stages))
        self._completed = 0
        self._state = "idle"  # idle, opening, ready, retry, ok, error
        self.set_content_width(180)
        self.set_content_height(180)
        self.set_draw_func(self._on_draw)

    @property
    def total_stages(self) -> int:
        return self._total

    @property
    def completed_stages(self) -> int:
        return self._completed

    def set_progress(self, completed: int, total: int | None = None, state: str = "idle") -> None:
        if total is not None:
            self._total = max(1, int(total))
        self._completed = max(0, min(int(completed), self._total))
        self._state = state
        self.queue_draw()

    def _on_draw(self, _area, cr, width: int, height: int) -> None:
        cx = width / 2.0
        cy = height / 2.0
        radius = min(width, height) * 0.40
        stroke = max(8.0, radius * 0.16)
        cr.set_line_width(stroke)

        gap = 0.10 if self._total > 1 else 0.0
        step = (2.0 * math.pi) / self._total
        start_offset = -math.pi / 2.0

        for idx in range(self._total):
            a0 = start_offset + idx * step + gap / 2.0
            a1 = start_offset + (idx + 1) * step - gap / 2.0
            if idx < self._completed:
                # Completed segment: green
                cr.set_source_rgba(0.18, 0.76, 0.49, 1.0)
            elif idx == self._completed and self._state == "ready":
                # Active listening segment: bright accent blue
                cr.set_source_rgba(0.21, 0.52, 0.89, 1.0)
            elif idx == self._completed and self._state == "retry":
                # Retry warning segment: amber
                cr.set_source_rgba(0.90, 0.65, 0.04, 1.0)
            elif self._state == "error":
                cr.set_source_rgba(0.88, 0.11, 0.14, 0.45)
            else:
                # Pending segment: muted neutral
                cr.set_source_rgba(0.55, 0.55, 0.58, 0.28)
            cr.arc(cx, cy, radius, a0, a1)
            cr.stroke()

        # Center counter text
        label = f"{self._completed}/{self._total}"
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(max(18.0, radius * 0.38))
        ext = cr.text_extents(label)
        cr.set_source_rgba(0.5, 0.5, 0.55, 0.95)
        cr.move_to(cx - ext.width / 2.0 - ext.x_bearing, cy - ext.height / 2.0 - ext.y_bearing)
        cr.show_text(label)
