# Documentation

| Document | Read it when you want to |
| --- | --- |
| [install.md](fte3600/install.md) | build the driver, install firmware, enable the service, roll back |
| [hardware-gpd-pocket3.md](fte3600/hardware-gpd-pocket3.md) | GPD Pocket 3 (G1621-02 / N6000) hardware profile, CS polarity, and shared power button |
| [architecture.md](fte3600/architecture.md) | see how the sensor, kernel glue, libfprint driver, fprintd, PAM, and toolkit fit together |
| [toolkit.md](fte3600/toolkit.md) | use or extend the CLI and GTK4/libadwaita toolbox that enrolls prints and measures recognition |
| [matcher-benchmark.md](fte3600/matcher-benchmark.md) | run the offline Docker multi-algorithm matcher benchmark (`BRISK` / `CLAHE+BRISK` / `AKAZE` / `SIGFM`) |
| [troubleshooting.md](fte3600/troubleshooting.md) | diagnose "nothing happens", "not recognised", "device busy", power-button, or suspend errors |

Repository policies live at the root: [CONTRIBUTING](../CONTRIBUTING.md),
[SECURITY](../SECURITY.md), [AUTHORS](../AUTHORS), [license](../COPYING).

## Status

| Part | State |
| --- | --- |
| ACPI reset/IRQ glue (`kernel/fte3600`) | confirmed on GPD Pocket 3 (N6000) & One-Netbook A1, ABI 2 |
| libfprint driver (`libfprint/drivers/fte3600`) | enroll, verify, identify, cancellable cold-open retry, bounded session-loss recovery, verify/identify suspend-resume |
| Match policy | strict and **uncalibrated** (`FTE3600_BRISK_THRESHOLDS_CALIBRATED 0`) |
| Toolbox (`toolkit/`) | CLI (`fte3600-toolkit`), GTK4 + libadwaita GUI (`fte3600-toolkit-gui`), and mock D-Bus E2E tests implemented |
| Arch packaging (`packaging/arch`) | packages libfprint driver, DKMS glue, udev/modprobe rules, and `fte3600-toolkit` |

Authentication stays experimental until the recognition rate is measured on
more than one machine and an impostor test exists. Keep password access enabled.

## Roadmap

| Milestone | Deliverable | Status |
| --- | --- | --- |
| M1 | Shared `Gio.DBus` core + CLI (`status`, `doctor`, `enroll`, `verify`, `report`, `list`, `delete`) + mock D-Bus E2E | Implemented & CI-tested |
| M2 | Single-window GTK4 + libadwaita desktop toolbox on the same core | Implemented |
| M3 | Threshold calibration (`>=30` genuine + `>=30` impostor rounds, justified policy constants) | In progress (collect telemetry with `fte3600-toolkit verify`) |
| M4 | Open-source release (CI, Arch packaging, supported-hardware table, release notes) | Ready |
