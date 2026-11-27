<div align="center">

# libfprint-fte3600

**Linux SPI driver, ACPI/DKMS kernel glue, and unprivileged GTK4 + CLI toolkit for FocalTech `FTE3600` fingerprint sensors**

[English](README.md) · [简体中文](README-zh.md)

[![CI](https://github.com/wings1848/libfprint-fte3600/actions/workflows/fte3600-ci.yml/badge.svg)](https://github.com/wings1848/libfprint-fte3600/actions/workflows/fte3600-ci.yml)
[![AUR version](https://img.shields.io/aur/version/libfprint-fte3600-git?label=AUR%20libfprint-fte3600-git)](https://aur.archlinux.org/packages/libfprint-fte3600-git)
[![License: LGPL-2.1-or-later](https://img.shields.io/badge/host%20%26%20toolkit-LGPL--2.1--or--later-blue.svg)](COPYING)
[![License: GPL-2.0-only](https://img.shields.io/badge/kernel%20glue-GPL--2.0--only-green.svg)](kernel/fte3600/fte3600.c)

[Quick Start](#quick-start) · [Clients](#clients--everyday-usage) · [Hardware Support](#hardware-support) · [Architecture](#components) · [Documentation](#documentation)

</div>

---

> [!IMPORTANT]
> - **SPI sensors only**: USB FocalTech readers are outside the scope of this driver.
> - **Experimental authentication**: BRISK verification thresholds remain uncalibrated for population-scale FAR/FRR (`FTE3600_BRISK_THRESHOLDS_CALIBRATED=0`). **Always retain password login.**
> - **No bundled firmware**: Chips that require external firmware must load user-supplied firmware files (see [install.md](docs/fte3600/install.md#firmware)).

## Overview

`libfprint-fte3600` brings FocalTech `FTE3600`-family SPI press-type fingerprint readers (found on GPD Pocket 3, One-Netbook A1, OneMix 4, ONEXPLAYER X2 Mini, and similar mini-laptops) to standard Linux `libfprint` / `fprintd` / PAM authentication.

Implemented driver profiles: `FT9338`, `FT9348`, `FT9361`, `FT9536`, `FT9365`, `FT9368`, `FW9369`, and `FT9769`.

## Components

| Component | Path & License | What it provides |
| --- | --- | --- |
| **ACPI / DKMS Kernel Glue** | [`kernel/fte3600`](kernel/fte3600) (`GPL-2.0-only`) | Minimal ABI 2 platform module exposing ACPI reset GPIO + UIO edge IRQ alongside stock distribution `spidev` (`bufsiz >= 65536`). |
| **`libfprint` SPI Driver** | [`libfprint/drivers/fte3600`](libfprint/drivers/fte3600) (`LGPL-2.1-or-later`) | Userspace SPI protocol, fast open for qualified legacy profiles (`FT9338`/`FT9348`/`FT9361`/`FT9536`), cancellable cold-open retry, release-timeout reopen cooldown, verify/identify `.suspend`/`.resume` handling, 12-sample template gallery, coverage enrollment for small (`<= 64×80`) sensors, and strict single-pass LCN `sam7-brisk` matching. |
| **Unprivileged Toolkit (CLI + GUI)** | [`toolkit/`](toolkit) (`LGPL-2.1-or-later`) | `fte3600-toolkit` CLI and single-window GTK4 + libadwaita `fte3600-toolkit-gui` over `fprintd` D-Bus: prompts to press only after `Claim` + `Start` succeed, acquires `systemd-logind` `handle-power-key` inhibitor on shared power-button readers, runs read-only diagnostics (`doctor`), and logs zero-biometric JSONL timings (`report`). |
| **Offline Matcher Benchmark Lab** | [`tools/fte3600-bench`](tools/fte3600-bench) (`LGPL-2.1-or-later`) | Self-contained Docker harness linking the real `sam7-brisk` C core to benchmark `BRISK`, `CLAHE+BRISK`, `AKAZE`, and `SIGFM(SIFT)` on synthetic or private `FTE3600_DUMP_DIR` captures without installing OpenCV on the host. |

## Hardware Support

| Device | Sensor / Chip | Status & Reference |
| --- | --- | --- |
| **GPD Pocket 3 (`G1621-02` / N6000)** | `FW9369`, raw ID `0x9362` (`64×80`, no external firmware) | Confirmed: 12-sample coverage enrollment, repeated verification, PAM `sudo`, S3 suspend/resume, and lock-screen unlock ([GPD Pocket 3 guide](docs/fte3600/hardware-gpd-pocket3.md), [upstream #2](https://github.com/SamSeven777/libfprint-fte3600/issues/2#issuecomment-6212129012)). |
| **One-Netbook A1** | `FT9361` | Hardware-confirmed on upstream's original test device. |
| **OneMix 4** | `FT9769`-class, chip `0x9391`, ACPI `FTE6600` | Re-enrollment and verification confirmed ([upstream #11](https://github.com/SamSeven777/libfprint-fte3600/pull/11)). |
| **ONEXPLAYER X2 Mini** | `FT9365`, chip `0x9365` | Enrollment and unlock reported working ([upstream #15](https://github.com/SamSeven777/libfprint-fte3600/pull/15)). |

Use `fte3600-tool` (see [hardware tools](docs/fte3600/tools.md)) to identify an unlisted SPI sensor and collect diagnostics.

## Quick Start

### Arch Linux (AUR — Recommended)

Install [`libfprint-fte3600-git`](https://aur.archlinux.org/packages/libfprint-fte3600-git) together with `fprintd` and headers for your running kernel (e.g. `linux-headers` or `linux-lts-headers`). Add `gtk4` and `libadwaita` if you want the desktop GUI:

```sh
paru -S libfprint-fte3600-git fprintd linux-headers
# Optional for the desktop GUI:
paru -S gtk4 libadwaita
```

Reboot so DKMS loads the `fte3600` module and `spidev` applies `bufsiz=65536`, then grant `fprintd` access to the paired device nodes and verify the setup:

```sh
sudo fte3600-device-allow --install
sudo systemctl restart fprintd.service
fte3600-toolkit doctor
```

### Build from Source (Fedora, Ubuntu, NixOS, and other distributions)

See [install.md](docs/fte3600/install.md) and [packaging/README.md](packaging/README.md) for full dependency lists and NixOS module instructions.

```sh
# 1. Install kernel glue and verify hardware pairing
sudo ./scripts/setup-fte3600.sh install-kernel
./scripts/fte3600-tool.sh check

# 2. Build and install libfprint + toolkit
meson setup build --prefix=/usr --buildtype=release -Ddrivers=all -Ddoc=false -Dgtk-examples=false
meson compile -C build
sudo meson install -C build
python3 -m pip install --no-build-isolation ./toolkit
```

## Clients & Everyday Usage

Once installed, three user-facing clients are available—no scripts or long flags required:

| Client | Best for | How to launch | Notes |
| --- | --- | --- | --- |
| **System Settings (KDE / GNOME) & PAM** | Daily unlock, `sudo`, lock screen | KDE System Settings → Users → Fingerprint Authentication | Works natively through standard `libfprint-2.so` / `fprintd`. |
| **Desktop GUI (`fte3600-toolkit-gui`)** | Guided enrollment, power-button guard, match-rate testing | Application menu → **FTE3600 Fingerprint Toolkit**, or run `fte3600-toolkit-gui` | Zero arguments; waits for sensor `READY` before prompting and holds a `handle-power-key` inhibitor on GPD hardware. |
| **CLI (`fte3600-toolkit`)** | Terminal & SSH workflows | `fte3600-toolkit doctor`<br>`fte3600-toolkit enroll`<br>`fte3600-toolkit verify`<br>`fte3600-toolkit report` | Sensible defaults (`right-index-finger`, 5 rounds). Read/verify commands require no password; `enroll`/`delete` trigger Polkit auth (use `sudo fte3600-toolkit enroll --user "$USER"` over pure SSH). |

## Verification & Testing

```sh
# C driver, syscall-wrapped runtime tests, fake-backend API tests, and kernel policy checks
./scripts/check-fte3600.sh

# Toolkit unit tests + isolated session-bus D-Bus protocol E2E tests
python3 -W error::ResourceWarning -m unittest discover -s toolkit/tests -p 'test_core_unit.py' -v
dbus-run-session -- python3 -W error::ResourceWarning -m unittest toolkit/tests/test_dbus_e2e.py toolkit/tests/test_gui_smoke.py -v

# Optional offline 4-matcher Docker benchmark (BRISK / CLAHE+BRISK / AKAZE / SIGFM)
./tools/fte3600-bench/run-docker.sh
```

## Documentation

| Document | Topic |
| --- | --- |
| [docs/README.md](docs/README.md) | Documentation overview and roadmap |
| [docs/fte3600/install.md](docs/fte3600/install.md) | Source installation, firmware placement, PAM setup, and rollback |
| [packaging/README.md](packaging/README.md) | Arch Linux AUR (`libfprint-fte3600-git`) and NixOS packaging |
| [docs/fte3600/hardware-gpd-pocket3.md](docs/fte3600/hardware-gpd-pocket3.md) | GPD Pocket 3 (`G1621-02` / N6000) hardware profile and shared power button |
| [docs/fte3600/toolkit.md](docs/fte3600/toolkit.md) | `fte3600-toolkit` CLI & GTK4/libadwaita GUI usage and telemetry schema |
| [docs/fte3600/matcher-benchmark.md](docs/fte3600/matcher-benchmark.md) | Offline Docker multi-algorithm matcher benchmark and real-hardware findings |
| [docs/fte3600/architecture.md](docs/fte3600/architecture.md) | End-to-end architecture across kernel glue, driver, `fprintd`, and toolkit |
| [docs/fte3600/tools.md](docs/fte3600/tools.md) | `fte3600-tool` hardware identification and open-latency benchmarking |
| [docs/fte3600/fast-open-validation.md](docs/fte3600/fast-open-validation.md) | Fast-open qualification criteria for legacy profiles |
| [docs/fte3600/troubleshooting.md](docs/fte3600/troubleshooting.md) | Troubleshooting guide (wakeup void, power button, Polkit/SSH, suspend) |

## Branches & Matching Algorithms

- `main` uses the single-pass LCN `sam7-brisk` matcher for enrollment, verification, and identification.
- Experimental IPA matcher sources remain on upstream [`research/algorithms`](https://github.com/SamSeven777/libfprint-fte3600/tree/research/algorithms).

## Licensing & Attribution

- Host driver and toolkit: `LGPL-2.1-or-later`
- Kernel ACPI/UIO glue (`kernel/fte3600`): `GPL-2.0-only`
- Based on upstream [libfprint](https://gitlab.freedesktop.org/libfprint/libfprint) and [SamSeven777/libfprint-fte3600](https://github.com/SamSeven777/libfprint-fte3600), incorporating the attributed SamSeven777 BRISK implementation. See [independent-source.json](docs/fte3600/independent-source.json), [COPYING](COPYING), and [`sam7-brisk/UPSTREAM.json`](libfprint/drivers/fte3600/sam7-brisk/UPSTREAM.json).
- Project policies: [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)

### Upstream Contributors

- [@FlightDan](https://github.com/FlightDan) · [@solarkennedy](https://github.com/solarkennedy) · [@ttamakijp](https://github.com/ttamakijp) · [@iwansugiarto](https://github.com/iwansugiarto) · [@Tuxman2](https://github.com/Tuxman2) · [@Kriketo](https://github.com/Kriketo) · [@Evaner5580](https://github.com/Evaner5580) · [@shokerplz](https://github.com/shokerplz)
