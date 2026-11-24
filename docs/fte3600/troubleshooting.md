# Troubleshooting

Start with the two commands that answer most questions:

```sh
sudo ./scripts/setup-fte3600.sh check
journalctl -u fprintd -b --no-pager | tail -40
```

## Triage

| Symptom | Likely cause | Check | Fix |
| --- | --- | --- | --- |
| First press after locking does nothing | device still waking up (~4 s), presses are dropped | note the time you pressed; an error line ~30 s later means the press was seen | press again, hold ~1 s |
| `verify-unknown-error` after ~30 s | no press reached the sensor inside the capture window | journal shows the error ~30 s after `Verify started` | press after the prompt, cover the whole pad |
| Usually not recognised, occasionally works | light touch, partial pad coverage, or matcher gates | compare firm full-pad presses against light taps | press firmly, re-enroll if unchanged |
| `Cannot run while suspended` | lock-screen scan raced a suspend or lid close | journal shows the message with `pam_fprintd` | wake first, then unlock; see below |
| `Device was already claimed` | another client holds the device | `pgrep -a fprintd-verify` | wait or stop the other client |
| Enrollment keeps asking again | sample inconsistent with the accepted ones | `enroll-retry-scan` repeats | lift fully, re-centre, press firmly |
| `enroll-data-full` | print storage is full | `fprintd-list "$USER"` | `fprintd-delete "$USER"` |
| `fprintd` shows inactive | it is socket activated | `systemctl status fprintd` | nothing, it starts on demand |
| No device at all | module, spidev buffer or glue status | `lsmod`, `cat /sys/module/spidev/parameters/bufsiz` | see [install](install.md) |

## Presses before the prompt

`Claim` alone takes 3.9-4.5 s on the validated machine. Anything pressed before
`Verify started` never reaches the driver, and the following capture window then
times out after about 30 s, which looks like a rejection.

At the default log level fprintd only records the outcome, so compare timestamps:
if the failure line appears about 30 s after you pressed, the press was seen and
the capture timed out; if the whole run failed within a few seconds, the press
arrived too early or the device was busy.

```sh
journalctl -u fprintd -b -o short-iso | grep -E "verify|Claim|Release"
```

The toolbox prints a timestamped line for every state, which makes this visible
without debug logging. Wait for the password field (or the toolbox prompt), then
press.

## Not recognised

The matcher is uncalibrated (`FTE3600_BRISK_THRESHOLDS_CALIBRATED 0`), so
borderline captures fail. In practice, on the validated machine:

| Press style | Result |
| --- | --- |
| firm press, whole pad covered, held ~1 s | consistently accepted |
| light tap on the edge of the pad | consistently rejected |

If firm presses still fail, re-enroll: the template stores eight samples, and a
template recorded with bad habits rejects good presses later.

```sh
fprintd-delete -f right-index-finger "$USER"
fprintd-enroll -f right-index-finger "$USER"
```

## Cannot run while suspended

`VERIFY` and `IDENTIFY` implement `.suspend` / `.resume`: the driver cancels the
waiting worker, closes the hardware runtime cleanly before sleep, and reopens it
on resume. Active `ENROLL` and `CAPTURE` operations still cancel via framework
fallback (`Cannot run while suspended`) if the machine suspends mid-capture.

```sh
journalctl -b -o short-iso | grep -E "Cannot run while suspended|ReleaseDevice"
```

If you still observe this error on an older installed library, rebuild and
reinstall the updated `libfprint` package and restart `fprintd`.

## Power button side effects

On machines where the sensor doubles as the power button (such as GPD Pocket 3),
every firm press also emits `KEY_POWER`. If logind or the desktop power manager
is configured to suspend/shut down on short press, an enrollment or test can
trigger a power dialog mid-scan.

```sh
grep -r HandlePowerKey /etc/systemd/logind.conf /etc/systemd/logind.conf.d 2>/dev/null
fte3600-toolkit doctor
```

`fte3600-toolkit` automatically acquires an unprivileged `systemd-logind`
`handle-power-key` inhibitor lock during active enrollment and verification
rounds, and `fte3600-toolkit doctor` checks KDE PowerDevil settings read-only.
For daily lock-screen/sudo use, set the power-key short-press action to
"Do nothing" in your desktop power settings (long-press hardware power-off still
works).

## Collecting a report

```sh
journalctl -u fprintd -b --no-pager > fprintd.log
grep -H . /sys/bus/platform/devices/fte3600-glue.*/fte3600_* > glue.txt
fte3600-toolkit doctor --json > doctor.json
fte3600-toolkit report --json > rounds.json
```

Attach those three files, the distribution, the kernel version and the reader
model. Do not attach fingerprint images or the print database.
