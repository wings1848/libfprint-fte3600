# Toolbox: enrollment and recognition testing

The sensor has no LED, no buzzer and no way to say "I am listening". A press
that arrives while the device is waking up is silently dropped, and a press the
matcher rejects looks exactly like a press that never happened. `fprintd-enroll`
and `fprintd-verify` print one line per event into a terminal, which is the
wrong place for a prompt you are supposed to react to.

The toolbox fixes the feedback problem and produces the numbers needed to
calibrate the matcher.

## Scope

| It does | It does not |
| --- | --- |
| Enroll a finger, showing one stage per accepted sample | Change PAM, polkit or lock-screen configuration |
| Run timed verify rounds and report a match rate | Replace `fprintd`; it is a client of it |
| Explain each failure in plain language | Touch `/dev`, SPI, GPIO or UIO |
| Store one JSON record per round | Train or ship a model; matching stays in the driver |
| Run as the logged-in user | Ask for root, or keep any secret |

Two front ends, one core:

```text
toolkit/
  src/fte3600_toolkit/
    core/   D-Bus session, round runner, result vocabulary, record store
    cli/    fte3600-toolkit       (scriptable, also usable over SSH)
    gui/    fte3600-toolkit-gui   (GTK4 + libadwaita, single window)
  tests/
```

The GUI exists because the prompt must be unmissable; the CLI exists because
automation and remote shells have no window. Both drive the same state machine.

## Interaction contract

The single rule that makes recognition feel reliable:

> Announce "press now" **only after** `Claim` and `VerifyStart` have returned.

Measured wake-up is 3.9-4.5 s ([architecture](architecture.md#measured-timing)),
so anything shown before that is a lie and the user presses into a void.

```text
idle --start--> opening --Claim+VerifyStart--> ready --press--> capturing --> verdict
                  ~4 s, "waking up"          sound + prompt                green/red
```

Per state the user sees and hears:

| State | Window | Sound | Meaning |
| --- | --- | --- | --- |
| `opening` | "Waking the sensor..." spinner | none | do not press yet |
| `ready` | "Press now" + finger outline, window raised | short ping, desktop notification | press and hold ~1 s |
| `capturing` | "Reading..." | none | finger down, keep still |
| `verdict` | green check / red cross + one sentence | ascending / low tone | lift, then next round |

Timing budget per round: `opening` ~4 s, `ready` until the user presses (no
timeout in the GUI), `capturing` 0.3-2 s, `verdict` 1 s.

Rules that keep it honest:

* One press per sample. The sensor is press-type: lift fully between rounds.
* Never auto-advance silently. A round ends with a visible verdict.
* A press during `opening` is discarded by the device, not by us; if it happens
  the GUI says "the sensor was still waking up, press again" rather than
  "not recognised".
* Stopping always runs `VerifyStop` + `Release`. Killing the window must not
  leave the device claimed.

## Result vocabulary

`VerifyStatus` and `EnrollStatus` carry machine strings. The toolbox maps them
once, in `core`, so both front ends speak the same language.

| Result | Message | What the user should do |
| --- | --- | --- |
| `verify-match` | Recognised | nothing |
| `verify-no-match` | Not recognised — press harder, cover the whole pad | adjust finger, retry |
| `verify-retry-scan` | Image was unclear, try again | lift and re-press |
| `verify-finger-not-centered` | Move the finger to the centre | adjust position |
| `verify-swipe-too-short` | Lift slower | not applicable to press sensors, kept for parity |
| `verify-unknown-error` after ~30 s | No finger detected | press again; check the sensor is not covered |
| `verify-disconnected` | Sensor went away | stop; report a bug with the log |
| `enroll-stage-passed` | Sample N of 8 accepted | lift and press again |
| `enroll-retry-scan` | Sample rejected, try again | change position slightly |
| `enroll-completed` | Enrollment finished | ready to verify |
| `enroll-data-full` | Storage full | delete an old print |

Progress total comes from the `num-enroll-stages` property (9 here: eight
samples plus fprintd's own duplicate check), never from a constant.

## CLI

```sh
fte3600-toolkit status
fte3600-toolkit verify --rounds 5
fte3600-toolkit verify --rounds 5 --finger right-index-finger --json
fte3600-toolkit enroll --finger right-index-finger
fte3600-toolkit report            # aggregate the stored rounds
```

| Command | Behaviour | Exit code |
| --- | --- | --- |
| `status` | device name, scan type, enrolled fingers, `num-enroll-stages` | 0, 2 if no device |
| `verify --rounds N` | N rounds, prompt per round, summary at the end | 0 all matched, 1 partial, 2 device error |
| `enroll --finger F` | enrollment with per-stage feedback | 0 completed, 1 failed, 2 device error |
| `report` | match rate, median round time, failure histogram | 0 |

`--json` makes machine-readable output the default for scripting; the human
line format stays the default. `--no-sound` disables audio for quiet use.

## Records

One JSON object per round, appended to
`${XDG_STATE_HOME:-$HOME/.local/state}/fte3600-toolkit/rounds.jsonl`:

```json
{"schema":1,"ts":"2026-11-24T18:02:11.312+08:00","mode":"verify","round":1,
 "finger":"right-index-finger","result":"verify-match","claim_ms":4120,
 "ready_ms":4155,"total_ms":9310}
```

The records are the input for [calibration](../README.md#roadmap). They contain
no image data and no template: only timings and result names.

## GUI

One window, three pages, no wizard, no tray icon.

1. **Status** — device name, scan type, enrolled prints, one big "Test" button.
2. **Enroll** — an eight-segment progress ring, one segment per accepted
   sample, plus the counter and the current instruction.
3. **Test** — the state machine above, large type, live match counter, and a
   final summary card with the match rate and failure breakdown.

Toolchain, measured on the target machine:

| Candidate | Result |
| --- | --- |
| GTK4 + libadwaita (PyGObject) | 4.24 / 1.10, window opens on Wayland |
| Qt6 widgets | `PyQt6` is installed without `QtWidgets`/`QtQuick`: unusable |
| Kirigami | needs `cmake` + ECM, neither present |

Defaults chosen for the first version:

* English UI strings in one dictionary, so a translation is a single file.
* Audio on, desktop notification on, both toggleable and remembered.
* No PAM integration, no password handling, no background daemon.
* Single instance: a second launch focuses the existing window.

## Acceptance

Machine-checkable acceptance commands (run as the logged-in user; protocol-level
equivalents run automatically in CI under `dbus-run-session`):

| Requirement | Command | Expected |
| --- | --- | --- |
| Environment health | `fte3600-toolkit doctor` | exit 0 on a configured host; reports ACPI, glue ABI 2, spidev bufsiz, fprintd, PowerDevil |
| Runs unprivileged | `fte3600-toolkit status` | exit 0, prints `FocalTech FTE3600` |
| Prompt follows readiness | `fte3600-toolkit verify --rounds 1` (do not touch) | `READY` line appears >= 3 s after start, then `verify-unknown-error` after ~30 s |
| Records every round | `wc -l < rounds.jsonl` after a 5-round run | grows by exactly 5 |
| Releases the device | `Ctrl-C` then `fprintd-list $USER` | returns in < 1 s, no "device busy" |
| Never needs root | same commands without `sudo` | no permission error |
| Summary arithmetic | `fte3600-toolkit report` | matches = matched records / total |
| GUI opens | `fte3600-toolkit-gui` | window on Wayland, `status` page populated |

## Milestones

| Milestone | Scope | Status |
| --- | --- | --- |
| M1 | `core` + `cli` + mock D-Bus E2E | Implemented & CI-verified |
| M2 | `gui` on the same core (GTK4 + libadwaita) | Implemented |
| M3 | calibration (`>= 30` genuine + `>= 30` impostor rounds) | Open — collect data via `fte3600-toolkit verify` |
| M4 | open-source release (CI, Arch PKGBUILD, hardware table, release notes) | Ready |
