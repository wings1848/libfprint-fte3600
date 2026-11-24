# SPDX-License-Identifier: LGPL-2.1-or-later
"""Read-only host diagnostics for FTE3600 / GPD systems (zero root required)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .backend import FprintBackend, FprintError
from .power import inspect_powerdevil_config


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: str  # "ok", "warn", "fail", "skip"
    detail: str
    hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def run_doctor(
    *,
    backend: FprintBackend | None = None,
    username: str = "",
    sysfs_root: Path | str = "/sys",
    dev_root: Path | str = "/dev",
    powerdevil_config: Path | None = None,
) -> list[CheckResult]:
    """Run non-destructive, unprivileged checks across ACPI, glue, spidev, fprintd, and PowerDevil."""
    sysfs = Path(sysfs_root)
    dev = Path(dev_root)
    results: list[CheckResult] = []

    # 1. ACPI FTE3600 presence
    acpi_dir = sysfs / "bus" / "acpi" / "devices"
    acpi_nodes = sorted(acpi_dir.glob("FTE3600:*")) if acpi_dir.is_dir() else []
    if not acpi_nodes:
        results.append(
            CheckResult(
                name="acpi",
                status="warn",
                detail="No FTE3600:* device under /sys/bus/acpi/devices",
                hint="If this machine uses a different fingerprint reader, fprintd may still work.",
            )
        )
    else:
        statuses = [f"{n.name}(status={_read_text(n / 'status') or '?'})" for n in acpi_nodes]
        all_enabled = all(_read_text(n / "status") == "15" for n in acpi_nodes)
        results.append(
            CheckResult(
                name="acpi",
                status="ok" if all_enabled else "warn",
                detail=", ".join(statuses),
                hint="" if all_enabled else "Expected ACPI status 15 (present & enabled).",
            )
        )

    # 2. Kernel glue ABI 2
    plat_dir = sysfs / "bus" / "platform" / "devices"
    glue_nodes = sorted(plat_dir.glob("fte3600-glue.*")) if plat_dir.is_dir() else []
    if not glue_nodes:
        results.append(
            CheckResult(
                name="kernel-glue",
                status="fail" if acpi_nodes else "skip",
                detail="No fte3600-glue platform device found",
                hint="Install kernel glue via `sudo ./scripts/setup-fte3600.sh install-kernel`.",
            )
        )
    else:
        g = glue_nodes[0]
        abi = _read_text(g / "fte3600_glue_abi")
        status = _read_text(g / "fte3600_status")
        speed = _read_text(g / "fte3600_acpi_speed_hz")
        irq_src = _read_text(g / "fte3600_irq_source")
        cs_ctrl = _read_text(g / "fte3600_cs_control")
        ok = abi == "2" and status == "ready"
        results.append(
            CheckResult(
                name="kernel-glue",
                status="ok" if ok else "fail",
                detail=(
                    f"{g.name}: abi={abi}, status={status}, "
                    f"speed_hz={speed}, cs_control={cs_ctrl}, irq={irq_src}"
                ),
                hint="" if ok else "Expected glue ABI 2 and status=ready; reboot after DKMS update.",
            )
        )

    # 3. spidev bufsiz >= 65536
    bufsiz_raw = _read_text(sysfs / "module" / "spidev" / "parameters" / "bufsiz")
    if bufsiz_raw is None:
        results.append(
            CheckResult(
                name="spidev-bufsiz",
                status="fail" if acpi_nodes else "skip",
                detail="spidev module not loaded",
                hint="Ensure spidev is loaded with bufsiz=65536 (config/modprobe.d/fte3600-acpi-spidev.conf).",
            )
        )
    else:
        try:
            bufsiz = int(bufsiz_raw)
        except ValueError:
            bufsiz = -1
        ok = bufsiz >= 65536
        results.append(
            CheckResult(
                name="spidev-bufsiz",
                status="ok" if ok else "fail",
                detail=f"bufsiz={bufsiz_raw}",
                hint="" if ok else "FTE3600 requires spidev bufsiz >= 65536; reload spidev or reboot.",
            )
        )

    # 4. /dev/fte3600-* paired symlinks
    dev_links = sorted(p.name for p in dev.glob("fte3600-*")) if dev.is_dir() else []
    has_spi = any(name.startswith("fte3600-spi-") for name in dev_links)
    has_gpio = any(name.startswith("fte3600-gpio-") for name in dev_links)
    has_irq = any(name.startswith("fte3600-irq-") for name in dev_links)
    if has_spi and has_gpio and has_irq:
        results.append(
            CheckResult(
                name="dev-nodes",
                status="ok",
                detail=", ".join(dev_links),
            )
        )
    else:
        results.append(
            CheckResult(
                name="dev-nodes",
                status="fail" if acpi_nodes else "skip",
                detail=f"Found: {', '.join(dev_links) or 'none'}",
                hint="Expected spi, gpio, and irq symlinks under /dev/fte3600-*. Run `sudo ./scripts/setup-fte3600.sh check`.",
            )
        )

    # 5. fprintd D-Bus availability
    if backend is not None:
        try:
            info = backend.get_default_device(username)
            results.append(
                CheckResult(
                    name="fprintd",
                    status="ok",
                    detail=(
                        f"{info.name} (scan={info.scan_type}, "
                        f"enroll_stages={info.num_enroll_stages}, "
                        f"enrolled={len(info.enrolled_fingers)})"
                    ),
                )
            )
        except FprintError as exc:
            results.append(
                CheckResult(
                    name="fprintd",
                    status="fail",
                    detail=f"{exc.code}: {exc.message}",
                    hint="Ensure fprintd is installed and running (`systemctl status fprintd`).",
                )
            )
    else:
        results.append(
            CheckResult(
                name="fprintd",
                status="skip",
                detail="Skipped D-Bus probe",
            )
        )

    # 6. KDE PowerDevil shared power-button advisory (GPD Pocket 3 ergonomics)
    pd = inspect_powerdevil_config(powerdevil_config)
    if not pd.config_present:
        results.append(
            CheckResult(
                name="power-button",
                status="ok",
                detail="No KDE PowerDevil config detected (logind inhibitor will still guard active sessions)",
            )
        )
    elif pd.may_trigger_power_menu:
        summary = ", ".join(f"{k}={v}" for k, v in pd.profiles.items())
        results.append(
            CheckResult(
                name="power-button",
                status="warn",
                detail=f"PowerButtonAction not 0 in all profiles ({summary})",
                hint=(
                    "On GPD devices where the sensor doubles as the power button, set "
                    "System Settings -> Power Management -> Power Button Action -> Do nothing."
                ),
            )
        )
    else:
        results.append(
            CheckResult(
                name="power-button",
                status="ok",
                detail="KDE PowerDevil PowerButtonAction=0 across AC/Battery/LowBattery",
            )
        )

    return results
