#!/usr/bin/env python3
"""Keep the recorder running across crashes, restarts and network outages.

Data collection is the long pole -- you need days of it, and a single dropped
connection at hour 30 should not cost you the run. This restarts the recorder
with exponential backoff and logs every restart so gaps in the dataset are
visible rather than silent.

    python scripts/record_forever.py

Stop with Ctrl-C. Safe to leave running indefinitely; snapshots append to a
file per UTC day.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MIN_BACKOFF = 5.0
MAX_BACKOFF = 300.0
# A run that survived this long is treated as healthy, so the next failure
# starts backing off from scratch rather than inheriting an old penalty.
HEALTHY_SECONDS = 120.0


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def log(message: str, path: Path | None) -> None:
    line = f"[{stamp()}] {message}"
    print(line, flush=True)
    if path:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--log", default="recorder-supervisor.log", help="restart log (not the data)"
    )
    parser.add_argument("--max-restarts", type=int, default=0, help="0 = unlimited")
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="stop each recorder run after N ticks (for smoke-testing the supervisor)",
    )
    args = parser.parse_args()

    log_path = Path(args.log) if args.log else None

    cmd = [sys.executable, "-m", "btcbot"]
    if args.config:
        cmd += ["--config", args.config]
    cmd.append("record")
    if args.data_dir:
        cmd += ["--data-dir", args.data_dir]
    if args.max_ticks is not None:
        cmd += ["--max-ticks", str(args.max_ticks)]

    backoff = MIN_BACKOFF
    restarts = 0
    child: subprocess.Popen | None = None
    stopping = False

    def handle_stop(signum, _frame):
        nonlocal stopping
        stopping = True
        log(f"received signal {signum}; shutting down", log_path)
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGINT, handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_stop)

    log(f"supervising: {' '.join(cmd)}", log_path)

    while not stopping:
        started = time.time()
        try:
            child = subprocess.Popen(cmd, cwd=ROOT)
            code = child.wait()
        except KeyboardInterrupt:
            break
        except OSError as exc:
            log(f"could not start recorder: {exc}", log_path)
            code = -1

        if stopping:
            break

        ran_for = time.time() - started
        log(f"recorder exited (code {code}) after {ran_for:.0f}s", log_path)

        if ran_for >= HEALTHY_SECONDS:
            backoff = MIN_BACKOFF

        restarts += 1
        if args.max_restarts and restarts >= args.max_restarts:
            log(f"reached --max-restarts={args.max_restarts}; stopping", log_path)
            break

        log(f"restarting in {backoff:.0f}s (restart #{restarts})", log_path)
        slept = 0.0
        while slept < backoff and not stopping:
            time.sleep(min(1.0, backoff - slept))
            slept += 1.0

        backoff = min(backoff * 2, MAX_BACKOFF)

    log("supervisor stopped", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
