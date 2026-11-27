#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
# Pure-local end-to-end enrollment & verification harness for FTE3600.
# Runs the freshly compiled build-local/libfprint-2.so.2 inside a temporary
# fprintd instance (no system install) while driving fte3600-toolkit / GUI as
# your normal unprivileged desktop user.
set -euo pipefail

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR="$REPO_DIR/build-local"
FPRINTD_BIN=""
for candidate in /usr/lib/fprintd /usr/libexec/fprintd; do
  if [ -x "$candidate" ]; then
    FPRINTD_BIN="$candidate"
    break
  fi
done

FINGER="right-index-finger"
ROUNDS=5
SKIP_ENROLL=0
GUI_MODE=0
REBUILD=0
UNINSTALL_OLD=0
LANG_OPT="auto"
KEEP_LOG=0

usage() {
  cat <<'EOF'
Usage: ./scripts/e2e-local-fte3600.sh [options]

Pure-local E2E test for the built FTE3600 driver + unprivileged toolkit (no install).

Options:
  --finger <name>   Finger slot to enroll & verify (default: right-index-finger)
  --rounds <N>      Number of verification rounds (default: 5)
  --skip-enroll     Skip enrollment and run verification only
  --gui             Launch GTK4/libadwaita GUI against the temporary local fprintd
  --rebuild         Recompile build-local/ before starting
  --uninstall-old   Replace installed libfprint-fte3600-personal-git with stock libfprint first
  --lang <mode>     UI language: auto | en | zh (default: auto)
  --keep-log        Preserve temporary /tmp/fte3600-e2e-* logs after exit
  -h, --help        Show this help message
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --finger) FINGER="${2:?missing value for --finger}"; shift 2 ;;
    --rounds) ROUNDS="${2:?missing value for --rounds}"; shift 2 ;;
    --skip-enroll) SKIP_ENROLL=1; shift ;;
    --gui) GUI_MODE=1; shift ;;
    --rebuild) REBUILD=1; shift ;;
    --uninstall-old) UNINSTALL_OLD=1; shift ;;
    --lang) LANG_OPT="${2:?missing value for --lang}"; shift 2 ;;
    --keep-log) KEEP_LOG=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -eq 0 ]; then
  echo "error: do not run this script with sudo; run as your normal user." >&2
  echo "       sudo is invoked internally ONLY for the temporary fprintd process." >&2
  exit 2
fi

if [ -z "$FPRINTD_BIN" ]; then
  echo "error: fprintd binary not found in /usr/lib/fprintd or /usr/libexec/fprintd" >&2
  exit 1
fi

if [ "$UNINSTALL_OLD" -eq 1 ]; then
  if command -v pacman >/dev/null 2>&1 && pacman -Q libfprint-fte3600-personal-git >/dev/null 2>&1; then
    echo "==> Replacing old libfprint-fte3600-personal-git with stock libfprint..."
    sudo pacman -S libfprint
  else
    echo "==> Old package libfprint-fte3600-personal-git is not installed (skipping)."
  fi
  if [ -e /etc/systemd/system/fprintd.service.d/10-fte3600-acpi-spidev.conf ]; then
    echo "==> Removing stale fprintd drop-in from old package..."
    sudo rm -f /etc/systemd/system/fprintd.service.d/10-fte3600-acpi-spidev.conf
    sudo systemctl daemon-reload
  fi
fi

# Verify kernel glue & spidev buffer are live in the running kernel.
# Note: /dev/fte3600-* udev symlinks are only required by systemd fprintd.service
# sandboxing; the local fprintd process discovers spidev/gpio/uio via sysfs.
GLUE_READY=0
for status_file in /sys/bus/platform/devices/fte3600-glue*/fte3600_status; do
  if [ -r "$status_file" ] && [ "$(cat "$status_file" 2>/dev/null)" = "ready" ]; then
    GLUE_READY=1
    break
  fi
done
if [ "$GLUE_READY" -ne 1 ]; then
  echo "error: fte3600-glue platform device is not ready in /sys/bus/platform/devices/." >&2
  echo "       If you rebooted after uninstalling the old package, install kernel glue only:" >&2
  echo "         sudo ./scripts/setup-fte3600.sh install-kernel" >&2
  exit 1
fi

BUFSIZ=$(cat /sys/module/spidev/parameters/bufsiz 2>/dev/null || echo 0)
if [ "${BUFSIZ:-0}" -lt 65536 ]; then
  echo "error: /sys/module/spidev/parameters/bufsiz=$BUFSIZ (need >= 65536)." >&2
  exit 1
fi

# Build local driver on demand (never touches /usr/lib).
if [ "$REBUILD" -eq 1 ] || [ ! -e "$BUILD_DIR/libfprint/libfprint-2.so.2" ]; then
  echo "==> Building local FTE3600 driver in $BUILD_DIR (no system install)..."
  if [ ! -d "$BUILD_DIR" ]; then
    meson setup "$BUILD_DIR" "$REPO_DIR" --buildtype=release -Ddrivers=fte3600 \
      -Dgtk-examples=false -Ddoc=false -Dintrospection=false -Dinstalled-tests=false
  fi
  meson compile -C "$BUILD_DIR"
fi

LOCAL_LIB_DIR=$(CDPATH= cd -- "$BUILD_DIR/libfprint" && pwd)
LOCAL_LIB_SO=$(readlink -f "$LOCAL_LIB_DIR/libfprint-2.so.2")
if [ ! -f "$LOCAL_LIB_SO" ]; then
  echo "error: local library missing: $LOCAL_LIB_DIR/libfprint-2.so.2" >&2
  exit 1
fi

TMP_DIR=$(mktemp -d -t fte3600-e2e-XXXXXX)
chmod 700 "$TMP_DIR"
FPRINTD_LOG="$TMP_DIR/fprintd.log"
STATE_FILE="$TMP_DIR/rounds.jsonl"
PID_FILE="$TMP_DIR/fprintd.pid"
touch "$FPRINTD_LOG" "$PID_FILE"
chmod 600 "$FPRINTD_LOG" "$PID_FILE"
FPRINTD_PID=""
WAS_ACTIVE=0

cleanup() {
  local rc=$?
  echo ""
  echo "==> Cleaning up temporary local fprintd..."
  if [ -n "$FPRINTD_PID" ]; then
    sudo kill -TERM "$FPRINTD_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      sudo kill -0 "$FPRINTD_PID" 2>/dev/null || break
      sleep 0.1
    done
    sudo kill -KILL "$FPRINTD_PID" 2>/dev/null || true
  fi
  if [ "$WAS_ACTIVE" -eq 1 ]; then
    sudo systemctl start fprintd.service 2>/dev/null || true
  fi
  if [ "$KEEP_LOG" -eq 1 ]; then
    echo "==> Logs preserved in: $TMP_DIR"
  else
    rm -rf "$TMP_DIR"
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

echo "==> Authenticating sudo (used ONLY to start/stop temporary local fprintd)..."
sudo -v

if systemctl is-active --quiet fprintd.service 2>/dev/null; then
  WAS_ACTIVE=1
  echo "==> Stopping system fprintd.service temporarily..."
  sudo systemctl stop fprintd.service
fi
# Ensure no idle system fprintd process holds the bus name.
sudo pkill -x fprintd 2>/dev/null || true
sleep 0.3

echo "==> Starting temporary local fprintd with:"
echo "    $LOCAL_LIB_SO"
sudo bash -c '
  LD_LIBRARY_PATH="$1" G_MESSAGES_DEBUG=libfprint-fte3600 "$2" --no-timeout >"$3" 2>&1 &
  echo $! > "$4"
' _ "$LOCAL_LIB_DIR" "$FPRINTD_BIN" "$FPRINTD_LOG" "$PID_FILE"

FPRINTD_PID=$(cat "$PID_FILE")
sleep 0.6

if ! sudo kill -0 "$FPRINTD_PID" 2>/dev/null; then
  echo "error: local fprintd exited immediately. Log:" >&2
  cat "$FPRINTD_LOG" >&2 || true
  exit 1
fi

# Hard proof that fprintd actually loaded our local build-local/libfprint-2.so.2.
if ! sudo grep -Fq "$LOCAL_LIB_SO" "/proc/$FPRINTD_PID/maps"; then
  echo "error: fprintd (pid $FPRINTD_PID) did NOT load $LOCAL_LIB_SO!" >&2
  sudo grep libfprint "/proc/$FPRINTD_PID/maps" >&2 || true
  exit 1
fi
echo "==> Confirmed loaded library: $LOCAL_LIB_SO (pid $FPRINTD_PID)"

# Wait for D-Bus name registration.
READY=0
for _ in $(seq 1 30); do
  if PYTHONPATH="$REPO_DIR/toolkit/src" python3 -m fte3600_toolkit status --json >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.25
done
if [ "$READY" -ne 1 ]; then
  echo "error: timed out waiting for local fprintd on system D-Bus. Log:" >&2
  cat "$FPRINTD_LOG" >&2 || true
  exit 1
fi

echo ""
echo "=== [1/4] Diagnostics & Device Status ==="
PYTHONPATH="$REPO_DIR/toolkit/src" python3 -m fte3600_toolkit doctor --lang "$LANG_OPT" || true
PYTHONPATH="$REPO_DIR/toolkit/src" python3 -m fte3600_toolkit status --lang "$LANG_OPT"

if [ "$GUI_MODE" -eq 1 ]; then
  echo ""
  echo "=== Launching GTK4/libadwaita GUI against local fprintd ==="
  PYTHONPATH="$REPO_DIR/toolkit/src" python3 -m fte3600_toolkit.gui.app \
    --state-file "$STATE_FILE" --lang "$LANG_OPT"
else
  if [ "$SKIP_ENROLL" -eq 0 ]; then
    echo ""
    echo "=== [2/4] Interactive Enrollment ($FINGER) ==="
    PYTHONPATH="$REPO_DIR/toolkit/src" python3 -m fte3600_toolkit enroll \
      --finger "$FINGER" --state-file "$STATE_FILE" --lang "$LANG_OPT"
  else
    echo ""
    echo "=== [2/4] Enrollment skipped (--skip-enroll) ==="
  fi

  echo ""
  echo "=== [3/4] Interactive Timed Verification ($ROUNDS rounds, finger=$FINGER) ==="
  PYTHONPATH="$REPO_DIR/toolkit/src" python3 -m fte3600_toolkit verify \
    --finger "$FINGER" --rounds "$ROUNDS" --state-file "$STATE_FILE" --lang "$LANG_OPT" || true
fi

echo ""
echo "=== [4/4] Session Telemetry Report & Driver Summary ==="
SUMMARY_FILE="/tmp/fte3600-e2e-last-summary.txt"
{
  echo "=== Local E2E Summary ($(date -Iseconds)) ==="
  echo "Library: $LOCAL_LIB_SO"
  echo "Finger: $FINGER | Rounds: $ROUNDS | GUI: $GUI_MODE"
  echo ""
  echo "=== Session Telemetry Report ==="
  PYTHONPATH="$REPO_DIR/toolkit/src" python3 -m fte3600_toolkit report \
    --mode all --state-file "$STATE_FILE" --lang "$LANG_OPT" || true
  echo ""
  echo "=== Driver Debug Summary (BRISK & Capture Telemetry) ==="
  grep -E "Captured frame|Frame [0-9]+ (matched|no match)|Retry [0-9]+|Sensor profile|session|fast" "$FPRINTD_LOG" || \
    echo "(no driver capture lines recorded)"
} | tee "$SUMMARY_FILE"
chmod 600 "$SUMMARY_FILE" 2>/dev/null || true
echo ""
echo "==> Non-biometric summary saved to: $SUMMARY_FILE"
