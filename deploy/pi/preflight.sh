#!/usr/bin/env bash
# Check whether this machine can actually run the recorder, BEFORE installing
# anything. Every check here is one that has a real failure mode behind it.
#
#   bash preflight.sh [DATA_DIR]
#
# Exits non-zero if something would stop the recorder working. Safe to re-run.

set -uo pipefail
DATA_DIR="${1:-}"
fail=0
warn=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  OK    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; fail=$((fail+1)); }
note() { printf '  WARN  %s\n' "$*"; warn=$((warn+1)); }

say "== hardware =="
if [ -r /sys/firmware/devicetree/base/model ]; then
  MODEL=$(tr -d '\0' < /sys/firmware/devicetree/base/model)
  say "  model: $MODEL"
else
  say "  model: unknown (not a Pi? that is fine)"
fi
ARCH=$(uname -m)
say "  arch : $ARCH"
case "$ARCH" in
  armv6l)
    note "ARMv6 (Pi 1 / Zero). Modern Python wheels are rarely built for this,
        so httpx/cryptography may have to compile from source, which can take
        hours and sometimes fails outright. TLS is also slow on this core.
        Workable in principle; expect a fight. A Pi 2 or newer avoids it."
    ;;
  armv7l|aarch64|x86_64) ok "architecture is well supported" ;;
  *) note "unrecognised architecture '$ARCH'; may still work" ;;
esac

MEM_KB=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$MEM_KB" -gt 0 ]; then
  say "  ram  : $((MEM_KB/1024)) MB"
  [ "$MEM_KB" -lt 400000 ] && note "under ~400MB RAM; should still fit, it is a small process"
fi

say ""
say "== python =="
PY=""
for cand in python3.12 python3.11 python3.10 python3.9 python3; do
  command -v "$cand" >/dev/null 2>&1 && { PY="$cand"; break; }
done
if [ -z "$PY" ]; then
  bad "no python3 found. Install it: sudo apt update && sudo apt install -y python3 python3-venv"
else
  V=$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
  say "  $PY -> $V"
  if "$PY" -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)'; then
    ok "python >= 3.9"
  else
    bad "python $V is too old. Needs 3.9+. Reflash with a current Raspberry Pi OS (Bookworm ships 3.11)."
  fi
  "$PY" -c 'import venv' 2>/dev/null && ok "venv module present" \
    || bad "venv missing: sudo apt install -y python3-venv"
fi

say ""
say "== clock =="
# Window timing is clock-dependent: settlement requires a spot reading within
# 30s of the window close, so a skewed clock silently voids windows. A Pi has
# no battery-backed RTC, so this matters more here than on a laptop.
if command -v timedatectl >/dev/null 2>&1; then
  SYNC=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo "")
  say "  $(timedatectl | sed -n '1,2p' | tr '\n' ' ')"
  [ "$SYNC" = "yes" ] && ok "clock is NTP-synchronised" \
    || bad "clock NOT synchronised. Fix: sudo timedatectl set-ntp true"
else
  note "timedatectl unavailable; verify the clock is NTP-synced another way"
fi

say ""
say "== network / API reachability =="
# The failure that wastes a weekend. Kalshi is a US-regulated exchange and
# api.binance.com returns 451 from the US, which is why the config uses the
# binance.vision mirror. A datacenter or non-US IP can fail either one.
if ! command -v curl >/dev/null 2>&1; then
  bad "curl missing: sudo apt install -y curl"
else
  K=$(curl -s -m 20 -o /dev/null -w '%{http_code}' \
      "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=1")
  [ "$K" = "200" ] && ok "Kalshi market data reachable (200)" \
                   || bad "Kalshi returned $K -- geo-blocked or offline. The recorder cannot work here."
  B=$(curl -s -m 20 -o /dev/null -w '%{http_code}' \
      "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT")
  [ "$B" = "200" ] && ok "Binance spot mirror reachable (200)" \
                   || bad "Binance mirror returned $B -- spot would be null on every snapshot."
fi

say ""
say "== storage =="
if [ -n "$DATA_DIR" ]; then
  mkdir -p "$DATA_DIR" 2>/dev/null || true
  if [ ! -w "$DATA_DIR" ]; then
    bad "data dir '$DATA_DIR' is not writable"
  else
    AVAIL_MB=$(df -Pm "$DATA_DIR" | awk 'NR==2{print $4}')
    SRC=$(df -P "$DATA_DIR" | awk 'NR==2{print $1}')
    say "  $DATA_DIR -> $SRC, ${AVAIL_MB} MB free"
    # Measured on the live recorder with three families at a 2s poll.
    DAYS=$((AVAIL_MB / 745))
    say "  at ~745 MB/day that is about ${DAYS} days of headroom"
    if [ "$DAYS" -lt 14 ]; then
      bad "under 14 days of space. You need ~13 days of windows before any result means anything."
    elif [ "$DAYS" -lt 45 ]; then
      note "under ~6 weeks of space; plan to rotate or archive"
    else
      ok "comfortable headroom"
    fi
    case "$SRC" in
      /dev/mmcblk*|/dev/root)
        note "this path is on the SD CARD. 745 MB/day of continuous appends is
        how Pi SD cards die, and a 32GB card fills in about six weeks.
        Strongly prefer a USB SSD/stick mounted somewhere like /mnt/data." ;;
      *) ok "not on the SD card" ;;
    esac
  fi
else
  note "no DATA_DIR given; pass one to check free space and the SD-card question"
fi

say ""
if [ "$fail" -gt 0 ]; then
  say "RESULT: $fail blocking problem(s), $warn warning(s). Fix the FAILs before installing."
  exit 1
fi
say "RESULT: no blocking problems ($warn warning(s)). Safe to run setup.sh."
