#!/usr/bin/env bash
# Install the recorder as a systemd service on this machine.
#
#   bash deploy/pi/setup.sh /mnt/data
#
# Idempotent: safe to re-run after a code update. Run preflight.sh first.

set -euo pipefail

DATA_DIR="${1:-}"
if [ -z "$DATA_DIR" ]; then
  echo "usage: bash deploy/pi/setup.sh /path/to/data-dir" >&2
  echo "  (put this on a USB SSD/stick, not the SD card -- ~745 MB/day)" >&2
  exit 2
fi

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
USER_NAME=$(id -un)
SERVICE=btcbot-recorder
UNIT=/etc/systemd/system/${SERVICE}.service

echo "repo     : $REPO"
echo "user     : $USER_NAME"
echo "data dir : $DATA_DIR"
echo

# --- refuse to create a second writer ---------------------------------------
# Every data corruption in this project has come from two processes writing one
# data directory. If something is already appending here, stop.
if [ -d "$DATA_DIR" ]; then
  RECENT=$(find "$DATA_DIR" -maxdepth 1 -name '*.jsonl' -newermt '-60 seconds' 2>/dev/null | head -1)
  if [ -n "$RECENT" ] && ! systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    echo "REFUSING: '$RECENT' was written in the last 60s by something that is not" >&2
    echo "this service. Two recorders on one data dir corrupt it. Stop the other" >&2
    echo "one first (including on any other machine writing here)." >&2
    exit 1
  fi
fi

mkdir -p "$DATA_DIR"

# --- python environment ------------------------------------------------------
PY=""
for cand in python3.12 python3.11 python3.10 python3.9 python3; do
  command -v "$cand" >/dev/null 2>&1 && { PY="$cand"; break; }
done
[ -n "$PY" ] || { echo "no python3 found" >&2; exit 1; }
"$PY" -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)' \
  || { echo "python must be 3.9+" >&2; exit 1; }

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "creating venv with $PY"
  "$PY" -m venv "$REPO/.venv"
fi
"$REPO/.venv/bin/python" -m pip install --upgrade pip >/dev/null
echo "installing dependencies (this is the slow part on an old Pi)"
"$REPO/.venv/bin/python" -m pip install -r "$REPO/requirements.txt"

# --- smoke test before installing the service --------------------------------
# Better to fail here, visibly, than to install a service that crash-loops.
echo
echo "smoke test: discovering markets..."
if ! "$REPO/.venv/bin/python" -m btcbot verify-venue 2>&1 | tail -20; then
  echo "verify-venue failed -- not installing the service." >&2
  exit 1
fi

# --- install the unit --------------------------------------------------------
TMP=$(mktemp)
sed -e "s|__REPO__|$REPO|g" \
    -e "s|__USER__|$USER_NAME|g" \
    -e "s|__DATA_DIR__|$DATA_DIR|g" \
    "$REPO/deploy/pi/${SERVICE}.service" > "$TMP"
sudo install -m 0644 "$TMP" "$UNIT"
rm -f "$TMP"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
sudo systemctl restart "$SERVICE"

echo
sleep 5
sudo systemctl --no-pager --full status "$SERVICE" | head -15
echo
echo "Done. Useful commands:"
echo "  journalctl -u $SERVICE -f          # watch it run"
echo "  systemctl status $SERVICE          # is it alive"
echo "  sudo systemctl stop $SERVICE       # stop before any replay/backfill"
echo
echo "Verify it is actually writing (expect all three families, ~2s apart):"
echo "  ls -la $DATA_DIR"
echo "  tail -1 $DATA_DIR/snapshots-*.jsonl | head -c 200"
