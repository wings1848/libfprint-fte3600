# GPD Pocket 3 (G1621-02 / N6000) Hardware Notes

Verified on GPD Pocket 3 (`G1621-02`, Intel Pentium Silver N6000, BIOS 1.15) running Linux 7.2 with CachyOS / Arch userspace.

## ACPI & Silicon Identity

| Property | Measured Value |
| --- | --- |
| ACPI path | `\_SB.PC00.SPI1.FPNT` (`_HID=FTE3600`, `_STA=0x0F`) |
| SPI controller | Intel SPI1 (`pxa2xx-spi`), `spi-FTE3600:00` |
| Chip family / raw ID | **FW9369** (raw ID `0x9362` at SRAM `0x1a8b`) |
| Image geometry | 64 × 80 grayscale, press-type |
| External firmware | None required (FW9369 runs without an external firmware blob) |
| SPI parameters | Mode 0 (`CPOL=0`, `CPHA=0`), ACPI speed 1 MHz, **active-high CS (`SPI_CS_HIGH`)** |
| Reset GPIO | ACPI `GpioIo` (output, pull-up), exposed as `fte3600-gpio-FTE3600:00` |
| Interrupt | ACPI `GpioInt` (edge, active-high), exposed as `fte3600-irq-FTE3600:00` (UIO) |

### Chip-Select Polarity Note

The DSDT declares `PolarityLow` on `SpiSerialBusV2`, but the FW9369 silicon only responds with active-high chip select (`SPI_CS_HIGH`). The driver's CS negotiation detects this automatically at open time — do not hardcode CS polarity per board.

### Note on Intel Core i7 Variants

Earlier community reports for Core i7 variants of the GPD Pocket 3 describe a different ACPI `_CRS` layout (`Interrupt()` / `FPTT` / `FPTM`). That variant is not yet hardware-confirmed with this glue; verify `fte3600-toolkit doctor` and `sudo ./scripts/setup-fte3600.sh check` before assuming parity.

## Ergonomics: Shared Power Button

On the GPD Pocket 3, the fingerprint pad is physically integrated into the power button. Pressing firmly enough for a full-pad capture emits `KEY_POWER`.

- **During enrollment and recognition testing**: `fte3600-toolkit` automatically requests an unprivileged `systemd-logind` `handle-power-key` inhibitor lock (no `sudo` needed).
- **In daily desktop use (KDE Plasma / PowerDevil)**: If firm presses open the shutdown menu, set **System Settings → Power Management → Power Button Action → Do nothing** (long-press hardware power-off remains unaffected). `fte3600-toolkit doctor` checks your PowerDevil profile read-only and reports if any profile still has a non-zero power-button action.

## Practical Capture Tips

- Wait until the prompt announces readiness (~4 s wake-up during `Claim`); presses during wake-up are dropped by the sensor.
- Press **firmly** to cover the whole 64×80 pad and hold for ~1 s. Light edge taps fail frequently until M3 threshold calibration is completed.
- Always retain password fallback in PAM (`pam_fprintd.so` as `sufficient`, never `required`).
