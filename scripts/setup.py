#!/usr/bin/env python3
"""Cross-platform local bootstrap: venv, dependencies, .env, sanity check.

Deliberately uses only the standard library and no shell-specific syntax, so
the same command works on Windows, macOS and Linux:

    python scripts/setup.py

Safe to re-run. It will not overwrite an existing .env.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
MIN_PYTHON = (3, 10)


class Colour:
    OK = "\033[92m"
    WARN = "\033[93m"
    FAIL = "\033[91m"
    DIM = "\033[2m"
    END = "\033[0m"

    @classmethod
    def disable(cls) -> None:
        cls.OK = cls.WARN = cls.FAIL = cls.DIM = cls.END = ""


if platform.system() == "Windows" and not os.getenv("WT_SESSION"):
    # Older Windows consoles render ANSI codes as literal garbage.
    Colour.disable()


def say(symbol: str, message: str, colour: str = "") -> None:
    print(f"{colour}{symbol}{Colour.END} {message}")


def ok(message: str) -> None:
    say("[ok]", message, Colour.OK)


def warn(message: str) -> None:
    say("[!]", message, Colour.WARN)


def fail(message: str) -> None:
    say("[x]", message, Colour.FAIL)


def venv_python() -> Path:
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def activate_hint() -> str:
    if platform.system() == "Windows":
        return r".venv\Scripts\activate"
    return "source .venv/bin/activate"


def check_python() -> bool:
    if sys.version_info < MIN_PYTHON:
        fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, "
            f"found {sys.version_info.major}.{sys.version_info.minor}"
        )
        print("    Install a newer Python from https://python.org and re-run.")
        return False
    ok(f"Python {sys.version_info.major}.{sys.version_info.minor}")
    return True


def make_venv() -> bool:
    if venv_python().exists():
        ok(f"virtualenv already exists at {VENV_DIR.name}")
        return True
    print(f"    creating virtualenv in {VENV_DIR} ...")
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)
    except Exception as exc:  # noqa: BLE001
        fail(f"could not create virtualenv: {exc}")
        if platform.system() == "Linux":
            print("    On Debian/Ubuntu you may need: sudo apt install python3-venv")
        return False
    ok(f"virtualenv created at {VENV_DIR.name}")
    return True


def install_deps(dev: bool) -> bool:
    py = str(venv_python())
    packages = ["httpx>=0.27", "PyYAML>=6.0", "cryptography>=42.0"]
    if dev:
        packages.append("pytest>=8.0")

    print("    installing dependencies (this can take a minute) ...")
    result = subprocess.run(
        [py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        warn(f"could not upgrade pip: {result.stderr.strip()[:200]}")

    result = subprocess.run(
        [py, "-m", "pip", "install", "--quiet", *packages],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail("dependency install failed:")
        print(result.stderr.strip()[:1000])
        return False
    ok(f"installed: {', '.join(p.split('>=')[0] for p in packages)}")
    return True


def make_env_file() -> bool:
    env_path = ROOT / ".env"
    example = ROOT / ".env.example"

    if env_path.exists():
        ok(".env already exists (not overwritten)")
        return True
    if not example.exists():
        warn(".env.example missing; skipping")
        return True

    env_path.write_text(example.read_text())
    ok("created .env from .env.example")
    print(f"    {Colour.DIM}Credentials are only needed to place orders.{Colour.END}")
    print(f"    {Colour.DIM}Kalshi market data is public: record and paper work empty.{Colour.END}")
    return True


def check_import() -> bool:
    result = subprocess.run(
        [str(venv_python()), "-c", "import btcbot; print(btcbot.__version__)"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if result.returncode != 0:
        fail("btcbot did not import:")
        print(result.stderr.strip()[:600])
        return False
    ok(f"btcbot {result.stdout.strip()} imports cleanly")
    return True


def run_self_test() -> bool:
    """Prove the whole pipeline works offline, before touching any network."""
    py = str(venv_python())
    sim_dir = ROOT / "data-sim"

    print("    running an offline end-to-end check ...")
    result = subprocess.run(
        [py, "-m", "btcbot", "simulate", "--data-dir", str(sim_dir), "--windows", "60"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if result.returncode != 0:
        fail("simulate failed:")
        print((result.stderr or result.stdout).strip()[:600])
        return False

    result = subprocess.run(
        [py, "-m", "btcbot", "backtest", "--data-dir", str(sim_dir)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if result.returncode != 0:
        fail("backtest failed:")
        print((result.stderr or result.stdout).strip()[:600])
        return False

    ok("offline simulate + backtest pipeline works")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-dev", action="store_true", help="skip pytest")
    parser.add_argument("--skip-selftest", action="store_true")
    args = parser.parse_args()

    print("\nbtcintervaltrader -- local setup\n" + "=" * 40)

    steps = [
        ("Python version", check_python),
        ("Virtualenv", make_venv),
        ("Dependencies", lambda: install_deps(dev=not args.no_dev)),
        ("Config", make_env_file),
        ("Import check", check_import),
    ]
    if not args.skip_selftest:
        steps.append(("Self test", run_self_test))

    for name, step in steps:
        print(f"\n{name}:")
        if not step():
            print(f"\n{Colour.FAIL}Setup stopped.{Colour.END} Fix the above and re-run.")
            return 1

    activate = activate_hint()
    print(
        f"""
{'=' * 40}
{Colour.OK}Setup complete.{Colour.END}

Activate the environment:

    {activate}

Then, in order:

    python -m btcbot verify-venue     # check you can reach Kalshi
    python -m btcbot record           # collect data -- leave running for DAYS
    python -m btcbot sweep            # test the rule on YOUR data
    python -m btcbot paper            # trade simulated money on live books

Step 2 is the one that takes real time. There is no shortcut: you cannot
evaluate any rule without your own recorded books.
"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
