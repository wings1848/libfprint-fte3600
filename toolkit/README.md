# fte3600-toolkit

Unprivileged enrollment, timed recognition feedback, and read-only host diagnostics for FTE3600 / GPD fingerprint readers, built directly on `fprintd`'s `net.reactivated.Fprint` D-Bus API. Nothing here opens `/dev/spidev`, GPIO, or UIO directly.

## Features

- **Honest readiness prompting**: Announces "press now" (visual + audio + desktop notification) **only after** `Claim` and `VerifyStart`/`EnrollStart` return, eliminating lost presses during the ~4 s sensor wake-up window.
- **Single shared core**: Both the scriptable CLI (`fte3600-toolkit`) and the single-window GTK4 + libadwaita GUI (`fte3600-toolkit-gui`) drive one `Gio.DBus` state machine with guaranteed `Stop` + `Release` cleanup on exit, `Ctrl-C`, or window close.
- **GPD power-button ergonomics**: Acquires an unprivileged `systemd-logind` `handle-power-key` inhibitor lock during active enrollment/testing and checks KDE PowerDevil settings read-only (`doctor`).
- **Privacy-preserving calibration telemetry**: Appends only timestamps, finger slot names, timings (`claim_ms`, `ready_ms`, `capture_ms`, `total_ms`), and fprintd result codes to `${XDG_STATE_HOME:-~/.local/state}/fte3600-toolkit/rounds.jsonl`. Zero images or templates are stored.

## Commands

```sh
fte3600-toolkit doctor                          # Read-only ACPI/glue/spidev/fprintd/PowerDevil check
fte3600-toolkit status                          # Reader name, scan type, dynamic num-enroll-stages, enrolled prints
fte3600-toolkit list                            # List enrolled fingers
fte3600-toolkit enroll --finger right-index-finger
fte3600-toolkit verify --rounds 5               # Timed rounds + summary + JSONL recording
fte3600-toolkit verify --rounds 5 --json --no-sound
fte3600-toolkit delete --finger right-index-finger
fte3600-toolkit report                          # Match rate, median/p95 latencies, failure histogram
fte3600-toolkit-gui                             # GTK4 + libadwaita single-window desktop app
```

## Running Without Installing

```sh
PYTHONPATH=toolkit/src python3 -m fte3600_toolkit.cli.main status
PYTHONPATH=toolkit/src python3 -m fte3600_toolkit.gui.app
```

## Running Tests

```sh
# Unit tests (no D-Bus daemon needed)
python3 -m unittest discover -s toolkit/tests -p 'test_core_unit.py' -v

# Real protocol-level D-Bus E2E tests (isolated session bus + mock_fprintd.py)
dbus-run-session -- python3 -m unittest toolkit/tests/test_dbus_e2e.py toolkit/tests/test_gui_smoke.py -v
```

## Requirements

- Python 3.11+ with PyGObject (`python-gobject` on Arch, `python3-gobject` on Fedora, `python3-gi` on Ubuntu/Debian).
- GTK4 + libadwaita (`gtk4`, `libadwaita`) for `fte3600-toolkit-gui`.
- `fprintd` running with the FTE3600 driver installed (see [docs/fte3600/install.md](../docs/fte3600/install.md)).
- No root, no PAM edits, no background daemon.
