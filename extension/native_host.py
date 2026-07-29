"""Native messaging host: bridges Firefox extension -> btcbot HTTP server."""
from __future__ import annotations

import json
import struct
import sys
import urllib.error
import urllib.request

SERVER = "http://127.0.0.1:8787"


def _read() -> dict | None:
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    length = struct.unpack("<I", raw)[0]
    return json.loads(sys.stdin.buffer.read(length))


def _write(obj: dict) -> None:
    data = json.dumps(obj, default=str).encode()
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main() -> None:
    while True:
        msg = _read()
        if msg is None:
            break
        path = msg.get("path", "")
        try:
            req = urllib.request.Request(SERVER + path, method=msg.get("method", "GET"))
            if msg.get("body"):
                req.data = json.dumps(msg["body"]).encode()
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
                _write({"ok": True, "data": body})
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read())
            except Exception:
                detail = {}
            _write({"ok": False, "error": detail.get("error", f"HTTP {e.code}")})
        except Exception as e:
            _write({"ok": False, "error": str(e)})


if __name__ == "__main__":
    main()
