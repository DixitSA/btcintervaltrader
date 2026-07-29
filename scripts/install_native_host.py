"""Install the btcbot native messaging host in the Windows registry."""
from __future__ import annotations

import sys
import winreg
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "extension" / "btcbot.native.json"
REG_KEY = r"Software\Mozilla\NativeMessagingHosts\btcbot.native"


def install() -> None:
    if not MANIFEST.exists():
        print(f"Manifest not found: {MANIFEST}")
        sys.exit(1)

    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY)
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(MANIFEST))
        winreg.CloseKey(key)
        print(f"Installed native messaging host at HKCU\\{REG_KEY}")
        print(f"  manifest: {MANIFEST}")
    except Exception as e:
        print(f"Failed: {e}")
        sys.exit(1)


def uninstall() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
        print(f"Removed HKCU\\{REG_KEY}")
    except FileNotFoundError:
        print("Not installed.")
    except Exception as e:
        print(f"Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--uninstall":
        uninstall()
    else:
        install()
