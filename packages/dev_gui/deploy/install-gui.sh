#!/usr/bin/env bash
# Install the Rig Map services: sfm-engine, sfm-gateway, and (optionally) the kiosk.
#
# Nothing is started, enabled or added to the desktop unless you ask for it:
#   ./install-gui.sh                      write the two units for this user (no sudo), reload systemd
#   ./install-gui.sh --enable             also start them at login (and at boot, with linger)
#   ./install-gui.sh --kiosk              also open the Rig Map full screen when the desktop starts
#   ./install-gui.sh --system             install system-wide instead (needs sudo)
#   ./install-gui.sh --uninstall          stop, disable and remove everything this script installed
#   ./install-gui.sh --dry-run            show what would happen, change nothing
#
# The DearPyGui app and the engine cannot run together (they share the CAN bus and
# the BNC lines). Stop the engine (systemctl --user stop sfm-engine) before run.py.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(dirname "$HERE")"
MODE=user PYTHON="$(command -v python3)" PORT=8080
ENABLE=0 KIOSK=0 UNINSTALL=0 DRY=0 UNIT_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --user) MODE=user ;;
    --system) MODE=system ;;
    --python) PYTHON="$2"; shift ;;
    --port) PORT="$2"; shift ;;
    --enable) ENABLE=1 ;;
    --kiosk) KIOSK=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --dry-run) DRY=1 ;;
    --unit-dir) UNIT_DIR="$2"; shift ;;   # write the units here and skip systemctl (used by the tests)
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$MODE" = system ]; then
  [ -n "$UNIT_DIR" ] || [ "$(id -u)" -eq 0 ] || { echo "--system needs root: run it with sudo" >&2; exit 1; }
  DEFAULT_DIR=/etc/systemd/system; SYSCTL="systemctl"; WANTED_BY=multi-user.target
  OWNER="${SUDO_USER:-$(id -un)}"
  SYSTEM_USER="User=$OWNER"$'\n'"Group=$(id -gn "$OWNER")"
else
  DEFAULT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; SYSCTL="systemctl --user"; WANTED_BY=default.target
  SYSTEM_USER=""
fi
TARGET="${UNIT_DIR:-$DEFAULT_DIR}"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/sfm-kiosk.desktop"

say() { echo "  $*"; }
run() { if [ "$DRY" = 1 ]; then say "[dry run] $*"; else "$@"; fi; }
use_systemctl() { [ -z "$UNIT_DIR" ]; }

render() {  # render <template> <destination>
  local dest="$2" text
  text="$(sed -e "s|@WORKDIR@|$WORKDIR|g" -e "s|@PYTHON@|$PYTHON|g" -e "s|@PORT@|$PORT|g" -e "s|@WANTED_BY@|$WANTED_BY|g" "$1")"
  text="${text//@SYSTEM_USER@/$SYSTEM_USER}"
  if [ "$DRY" = 1 ]; then say "[dry run] would write $dest"; else mkdir -p "$(dirname "$dest")"; printf '%s\n' "$text" > "$dest"; say "wrote $dest"; fi
}

if [ "$UNINSTALL" = 1 ]; then
  echo "Removing the Rig Map services ($MODE)"
  if use_systemctl; then
    run $SYSCTL disable --now sfm-gateway.service sfm-engine.service 2>/dev/null || true
  fi
  for f in sfm-gateway.service sfm-engine.service; do [ -e "$TARGET/$f" ] && run rm -f "$TARGET/$f"; done
  [ -e "$AUTOSTART" ] && run rm -f "$AUTOSTART"
  use_systemctl && run $SYSCTL daemon-reload
  echo "Done. Your logs and ~/.sfm settings were not touched."
  exit 0
fi

echo "Installing the Rig Map services ($MODE), python: $PYTHON, port: $PORT"
[ -x "$PYTHON" ] || { echo "python not found: $PYTHON" >&2; exit 1; }
"$PYTHON" -c 'import aiohttp, can' 2>/dev/null || echo "  WARNING: $PYTHON cannot import aiohttp and python-can; the services would not start (see requirements.txt)"

render "$HERE/sfm-engine.service.in"  "$TARGET/sfm-engine.service"
render "$HERE/sfm-gateway.service.in" "$TARGET/sfm-gateway.service"
if [ "$KIOSK" = 1 ]; then
  render "$HERE/sfm-kiosk.desktop.in" "$AUTOSTART"
fi
use_systemctl && run $SYSCTL daemon-reload

if [ "$ENABLE" = 1 ] && use_systemctl; then
  run $SYSCTL enable sfm-engine.service sfm-gateway.service
  if [ "$MODE" = user ]; then
    say "To start them at boot, before anyone logs in:  loginctl enable-linger $(id -un)"
  fi
fi

echo
echo "Next steps:"
say "Start now:        $SYSCTL start sfm-engine sfm-gateway"
say "Open the page:    http://127.0.0.1:$PORT/"
say "Watch the logs:   journalctl $( [ "$MODE" = user ] && echo --user ) -u sfm-engine -u sfm-gateway -f"
[ "$ENABLE" = 1 ] || say "Start at login:   re-run with --enable"
[ "$KIOSK" = 1 ] || say "Full-screen kiosk: re-run with --kiosk (uses ~/.config/autostart)"
