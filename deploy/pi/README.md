# Running the recorder on a Raspberry Pi

Moves 24/7 data collection off the laptop. The recorder is a small HTTP polling
loop — roughly 11 requests every 2 seconds, negligible CPU — so the Pi's speed is
not the constraint. **Storage is.**

## Before you buy or plug in anything

Two things decide whether this is worth doing:

**1. Which Pi is it?** Run this on the Pi (or read the board):

```bash
cat /sys/firmware/devicetree/base/model; echo; uname -m
```

- `armv7l` / `aarch64` (Pi 2, 3, 4, 5, Zero 2) — fine.
- `armv6l` (original Pi 1, Pi Zero W) — workable but expect a fight. Python
  wheels are rarely built for ARMv6, so `httpx` and `cryptography` may compile
  from source over several hours and can fail. TLS is also slow on that core.
  If you have anything newer, use it instead.

**2. Where will the data live?** The recorder writes about **745 MB/day** with
three families at a 2-second poll — 22 GB/month. Do **not** put that on the SD
card: a 32 GB card fills in roughly six weeks, and continuous appends are the
classic way Pi SD cards die. Use a USB SSD or stick.

```bash
lsblk                                  # find the USB device, e.g. sda1
sudo mkdir -p /mnt/data
sudo mount /dev/sda1 /mnt/data
sudo chown "$USER" /mnt/data
```

To make that survive a reboot, add it to `/etc/fstab` by UUID (`sudo blkid` to
get the UUID):

```
UUID=xxxx-xxxx  /mnt/data  ext4  defaults,nofail  0  2
```

`nofail` matters — without it the Pi refuses to boot if the drive is missing.

## Getting the Pi on the network, headless

If the Pi has no OS yet, use **Raspberry Pi Imager** on your laptop. Choose
Raspberry Pi OS Lite (64-bit if the board supports it), then open the gear /
"Edit settings" before writing and set:

- hostname (e.g. `btcpi`)
- username + password
- Wi-Fi SSID and password, and your country
- **Enable SSH** (password authentication is fine to start)

That produces a Pi that joins your network on first boot with no monitor or
keyboard. Then from the laptop:

```bash
ssh <username>@btcpi.local
```

Wired ethernet is more reliable than Wi-Fi for something meant to run unattended.

## Install

```bash
git clone https://github.com/DixitSA/btcintervaltrader.git
cd btcintervaltrader
bash deploy/pi/preflight.sh /mnt/data     # checks the things that actually break
bash deploy/pi/setup.sh /mnt/data         # venv, deps, smoke test, systemd service
```

`preflight.sh` checks Python version, clock sync, free space, whether the path is
on the SD card, and — most importantly — whether Kalshi and Binance will actually
serve this machine. A `451` or `403` there is the failure that wastes a weekend.

`setup.sh` refuses to run if something else is already writing the data
directory, because two recorders on one directory corrupt it.

## Running it

```bash
journalctl -u btcbot-recorder -f      # watch it
systemctl status btcbot-recorder      # is it alive
sudo systemctl stop btcbot-recorder   # stop it (do this before any replay)
```

systemd is the supervisor here, so `scripts/record_forever.py` is not used. The
unit restarts on any exit, waits for the network *and* for the clock to be
NTP-synced, and never gives up after repeated failures.

## Cutting over from the laptop

Do these in one sitting. The rule that matters: **exactly one recorder, ever.**

1. On the laptop, stop and delete the scheduled task so it cannot come back:
   ```powershell
   Stop-ScheduledTask -TaskName BTCIntervalTrader-Recorder
   Unregister-ScheduledTask -TaskName BTCIntervalTrader-Recorder -Confirm:$false
   ```
2. Confirm nothing is still recording (no `python -m btcbot record` processes).
3. Copy the existing data across, e.g.
   `scp -r data/*.jsonl <user>@btcpi.local:/mnt/data/`
4. Start the Pi service.

Snapshot files are named per UTC day. If both machines record the same day you
get colliding filenames that cannot simply be merged — another reason to cut over
cleanly rather than running both "just in case".

## Watching it from the laptop

The control panel binds `127.0.0.1` by default. To reach it from another machine
on your LAN:

```bash
python -m btcbot serve --host 0.0.0.0 --data-dir /mnt/data-panel
```

Pass a **different** `--data-dir` than the recorder's. Starting a paper session
from the panel writes that directory, and pointing it at the recorder's data is
the two-writer case again. The panel cannot place real orders regardless.
