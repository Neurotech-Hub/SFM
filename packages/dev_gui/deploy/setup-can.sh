#!/usr/bin/env bash
set -euo pipefail

# One-shot setup for the SFM base station CAN HAT on Raspberry Pi OS.
#
# Usage:
#   sudo ./packages/dev_gui/deploy/setup-can.sh            # install + configure
#   sudo ./packages/dev_gui/deploy/setup-can.sh --verify   # check status only (no changes)
#
# The device tree overlay only takes effect after a reboot. If this is the
# first run on a fresh Pi, the script will append the overlay and reboot
# automatically. Run it a second time (or run --verify) after the Pi comes
# back up to confirm can0 is healthy.

if [[ $(id -u) -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo "$0" "$@"
  fi
  echo "This script must be run as root. Try: sudo $0" >&2
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CONFIG_FILE=/boot/firmware/config.txt
APPEND_FILE="$SCRIPT_DIR/boot-config-can.append.txt"
NETWORK_UNIT=/etc/systemd/network/80-can.network
NETWORK_SOURCE="$SCRIPT_DIR/80-can.network"
BITRATE=250000

# --- verification ------------------------------------------------------------

check() {
  local desc=$1 status=$2
  if [[ "$status" -eq 0 ]]; then
    printf '  [PASS] %s\n' "$desc"
  else
    printf '  [FAIL] %s\n' "$desc"
  fi
}

run_verify() {
  local failures=0 rc=0 can0_present=1

  grep -q '^dtparam=spi=on' "$CONFIG_FILE" 2>/dev/null && rc=0 || rc=1
  check "SPI enabled in $CONFIG_FILE" "$rc"; [[ "$rc" -eq 0 ]] || failures=$((failures + 1))

  grep -q 'mcp2515-can0' "$CONFIG_FILE" 2>/dev/null && rc=0 || rc=1
  check "MCP2515 overlay present in $CONFIG_FILE" "$rc"; [[ "$rc" -eq 0 ]] || failures=$((failures + 1))

  lsmod | grep -q '^mcp251x' && rc=0 || rc=1
  check "mcp251x kernel module loaded" "$rc"; [[ "$rc" -eq 0 ]] || failures=$((failures + 1))

  [[ -f "$NETWORK_UNIT" ]] && rc=0 || rc=1
  check "systemd-networkd unit installed ($NETWORK_UNIT)" "$rc"; [[ "$rc" -eq 0 ]] || failures=$((failures + 1))

  systemctl is-active --quiet systemd-networkd && rc=0 || rc=1
  check "systemd-networkd service active" "$rc"; [[ "$rc" -eq 0 ]] || failures=$((failures + 1))

  command -v candump >/dev/null 2>&1 && rc=0 || rc=1
  check "can-utils installed (candump)" "$rc"; [[ "$rc" -eq 0 ]] || failures=$((failures + 1))

  ip link show can0 >/dev/null 2>&1 && rc=0 || rc=1
  check "can0 interface exists" "$rc"; [[ "$rc" -eq 0 ]] || { failures=$((failures + 1)); can0_present=0; }

  if [[ "$can0_present" -eq 1 ]]; then
    ip -details link show can0 | grep -q 'state UP' && rc=0 || rc=1
    check "can0 is UP" "$rc"; [[ "$rc" -eq 0 ]] || failures=$((failures + 1))

    ip -details link show can0 | grep -q "bitrate $BITRATE" && rc=0 || rc=1
    check "can0 bitrate is $BITRATE" "$rc"; [[ "$rc" -eq 0 ]] || failures=$((failures + 1))
  fi

  echo
  if [[ "$failures" -eq 0 ]]; then
    echo "All checks passed. CAN is ready."
  else
    echo "$failures check(s) failed."
    echo "If you just ran setup on a fresh Pi, reboot and re-run: sudo $0 --verify"
  fi
  return "$failures"
}

if [[ "${1:-}" == "--verify" ]]; then
  rc=0
  run_verify || rc=$?
  exit "$rc"
fi

# --- setup ---------------------------------------------------------------

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Boot config not found at $CONFIG_FILE" >&2
  exit 1
fi

NEEDS_REBOOT=0
if ! grep -q 'mcp2515-can0' "$CONFIG_FILE"; then
  echo "Appending MCP2515 device tree overlay to $CONFIG_FILE"
  printf '\n' >> "$CONFIG_FILE"
  cat "$APPEND_FILE" >> "$CONFIG_FILE"
  NEEDS_REBOOT=1
fi

mkdir -p /etc/systemd/network
cp "$NETWORK_SOURCE" "$NETWORK_UNIT"

if ! command -v candump >/dev/null 2>&1; then
  echo "Installing SocketCAN utilities"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq can-utils
fi

echo "Enabling systemd-networkd"
systemctl enable --now systemd-networkd || true
systemctl restart systemd-networkd || true

modprobe can-dev || true
modprobe mcp251x || true

if [[ "$NEEDS_REBOOT" -eq 1 ]] || ! ip link show can0 >/dev/null 2>&1; then
  echo
  echo "The overlay has been installed but the kernel needs a reboot to apply it."
  echo "Rebooting now. After the Pi comes back up, run:"
  echo "  sudo $0 --verify"
  reboot
  exit 0
fi

ip link set can0 up type can bitrate "$BITRATE" || true
echo "can0 is available."
echo
rc=0
run_verify || rc=$?
exit "$rc"
