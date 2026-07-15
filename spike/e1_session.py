#!/usr/bin/env python3
"""E1: Session detection matrix (XDG_SESSION_TYPE, WAYLAND_DISPLAY, DISPLAY)."""

from __future__ import annotations

import os
import platform
import subprocess


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        out = (completed.stdout + completed.stderr).strip()
        return completed.returncode, out
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"


def main() -> int:
    env = {
        "XDG_SESSION_TYPE": os.environ.get("XDG_SESSION_TYPE", ""),
        "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", ""),
        "DISPLAY": os.environ.get("DISPLAY", ""),
        "XDG_CURRENT_DESKTOP": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "DESKTOP_SESSION": os.environ.get("DESKTOP_SESSION", ""),
    }
    print("=== E1 session environment ===")
    for key, value in env.items():
        print(f"{key}={value!r}")

    print("\n=== derived session class ===")
    if env["WAYLAND_DISPLAY"]:
        session_class = "wayland"
    elif env["DISPLAY"]:
        session_class = "x11"
    else:
        session_class = "unknown"

    xdg = env["XDG_SESSION_TYPE"].lower()
    if xdg == "wayland":
        session_class = "wayland"
    elif xdg == "x11":
        session_class = "x11"

    print(f"session_class={session_class}")
    print(f"platform={platform.platform()}")

    print("\n=== loginctl session Type (if available) ===")
    code, out = _run(["loginctl", "show-session", "self", "-p", "Type"])
    print(f"exit={code}")
    print(out or "(empty)")

    print("\n=== KWin / compositor hints ===")
    for cmd in (
        ["pgrep", "-a", "kwin"],
        ["pgrep", "-a", "Xorg"],
        ["pgrep", "-a", "Xwayland"],
    ):
        code, out = _run(cmd)
        print(f"$ {' '.join(cmd)} -> exit={code}")
        print(out or "(no matches)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
