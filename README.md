# FTE3600 Fingerprint Driver & Toolkit for Linux (GPD / Mini-Laptops)

Linux SPI driver, ACPI reset/IRQ kernel glue, and unprivileged CLI + GTK4/libadwaita toolkit for FocalTech `FTE3600` press-type fingerprint sensors, with host-side BRISK enrollment, verification, and identification through `libfprint` / `fprintd`.

[Documentation](docs/README.md) · [Installation](docs/fte3600/install.md) · [Arch / NixOS](packaging/README.md) · [GPD Pocket 3 Guide](docs/fte3600/hardware-gpd-pocket3.md) · [Toolkit (CLI & GUI)](docs/fte3600/toolkit.md) · [Hardware tools](docs/fte3600/tools.md) · [Architecture](docs/fte3600/architecture.md) · [Troubleshooting](docs/fte3600/troubleshooting.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

Implemented profiles: `FT9338`, `FT9348`, `FT9361`, `FT9536`, `FT9365`, `FT9368`, `FW9369`, and `FT9769`.
USB sensors are outside this driver. Authentication thresholds remain experimental and uncalibrated (`FTE3600_BRISK_THRESHOLDS_CALIBRATED=0`); **always retain password login**.

## Components

1. **Kernel Glue (`kernel/fte3600`, GPL-2.0-only)** — Minimal DKMS platform glue (ABI 2) exposing ACPI reset GPIO + UIO edge IRQ alongside stock distribution `spidev` (`bufsiz >= 65536`).
2. **libfprint Driver (`libfprint/drivers/fte3600`, LGPL-2.1-or-later)** — Userspace SPI protocol, fast open for qualified legacy profiles, cancellable cold-open calibration retry, bounded session-loss recovery, verify/identify `.suspend`/`.resume` handling, and host-side BRISK matching. Firmware is never bundled.
3. **Unprivileged Toolkit (`toolkit/`, LGPL-2.1-or-later)** — CLI (`fte3600-toolkit`) and single-window GTK4 + libadwaita GUI (`fte3600-toolkit-gui`) sharing one `Gio.DBus` state machine over `fprintd`:
   - Announces "press now" **only after** `Claim` + `VerifyStart`/`EnrollStart` return (solving the wake-up void on LED-less readers),
   - Automatically acquires an unprivileged `systemd-logind` `handle-power-key` inhibitor on GPD devices where the sensor shares the power button,
   - Provides read-only environment diagnostics (`fte3600-toolkit doctor`),
   - Appends privacy-preserving JSONL timings (`rounds.jsonl`, zero biometric data) and aggregates match rates (`fte3600-toolkit report`) for future matcher calibration.

## Branches and matching algorithms

`main` uses BRISK for enrollment, verification and identification. IPA matcher sources and their standalone tests are maintained on upstream [`research/algorithms`](https://github.com/SamSeven777/libfprint-fte3600/tree/research/algorithms).
Fast open qualifies running `FT9338`, `FT9348`, `FT9361` and `FT9536` applications; other profiles use full initialization (see [fast-open validation](docs/fte3600/fast-open-validation.md)).

## Hardware reports

| Device | Sensor | Reported results |
| --- | --- | --- |
| One-Netbook A1 | FT9361 | Hardware-confirmed on upstream's original test device. |
| GPD Pocket 3 (`G1621-02` / N6000) | FW9369, raw ID `0x9362` (64×80, no external firmware) | Enrollment, repeated verification, PAM `sudo`, S3 suspend/resume, and lock-screen unlock confirmed ([GPD Pocket 3 guide](docs/fte3600/hardware-gpd-pocket3.md), [upstream report](https://github.com/SamSeven777/libfprint-fte3600/issues/2#issuecomment-6212129012)). |
| OneMix 4 | FT9769-class, chip `0x9391`, ACPI `FTE6600` | Re-enrollment and verification confirmed in upstream [#11](https://github.com/SamSeven777/libfprint-fte3600/pull/11). |
| ONEXPLAYER X2 Mini | FT9365, chip `0x9365` | Enrollment and unlock reported working in upstream [#15](https://github.com/SamSeven777/libfprint-fte3600/pull/15). |

Use the [hardware tool](docs/fte3600/tools.md) to identify a sensor and collect a report.

## Quick Start

```sh
# 1. Install kernel glue & libfprint driver (see docs/fte3600/install.md or packaging/README.md)
sudo ./scripts/setup-fte3600.sh install-kernel
./scripts/fte3600-tool.sh check

# 2. Run unprivileged toolkit diagnostics, enrollment, and timed verification
PYTHONPATH=toolkit/src python3 -m fte3600_toolkit doctor
PYTHONPATH=toolkit/src python3 -m fte3600_toolkit status
PYTHONPATH=toolkit/src python3 -m fte3600_toolkit enroll --finger right-index-finger
PYTHONPATH=toolkit/src python3 -m fte3600_toolkit verify --rounds 5
PYTHONPATH=toolkit/src python3 -m fte3600_toolkit report

# 3. Launch GTK4 + libadwaita desktop GUI
PYTHONPATH=toolkit/src python3 -m fte3600_toolkit.gui.app
```

## Verification & Testing

```sh
# C driver, syscall-wrapped runtime tests, fake-backend API tests, and kernel policy checks
./scripts/check-fte3600.sh

# Toolkit unit tests + real D-Bus protocol-level E2E tests (isolated session bus)
python3 -W error::ResourceWarning -m unittest discover -s toolkit/tests -p 'test_core_unit.py' -v
dbus-run-session -- python3 -W error::ResourceWarning -m unittest toolkit/tests/test_dbus_e2e.py toolkit/tests/test_gui_smoke.py -v
```

## Licensing & Attribution

The driver requires the ACPI reset/IRQ module in `kernel/fte3600`, distribution `spidev`, and the [firmware required by the chip](docs/fte3600/install.md#firmware).

Host code and toolkit are `LGPL-2.1-or-later`; kernel glue is `GPL-2.0-only`. Based on upstream [libfprint](https://gitlab.freedesktop.org/libfprint/libfprint) and [SamSeven777/libfprint-fte3600](https://github.com/SamSeven777/libfprint-fte3600), using the attributed SamSeven777 BRISK implementation. See [source origins](docs/fte3600/independent-source.json), `COPYING`, and `libfprint/drivers/fte3600/sam7-brisk/UPSTREAM.json`.

## Upstream Contributors

- [@FlightDan](https://github.com/FlightDan)
- [@solarkennedy](https://github.com/solarkennedy)
- [@ttamakijp](https://github.com/ttamakijp)
- [@iwansugiarto](https://github.com/iwansugiarto)
- [@Tuxman2](https://github.com/Tuxman2)
- [@Kriketo](https://github.com/Kriketo)
- [@Evaner5580](https://github.com/Evaner5580)
- [@shokerplz](https://github.com/shokerplz)
