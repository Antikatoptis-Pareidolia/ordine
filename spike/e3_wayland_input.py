#!/usr/bin/env python3
"""E3: Wayland input capture probes — portal D-Bus + evdev."""

from __future__ import annotations

import grp
import os
import pwd
import subprocess
from pathlib import Path


def _run(cmd: list[str]) -> tuple[int, str]:
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out = (completed.stdout + completed.stderr).strip()
    return completed.returncode, out


def _portal_probe() -> None:
    print("=== E3a xdg-desktop-portal D-Bus probe (gdbus) ===")
    code, out = _run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.DBus",
            "--object-path",
            "/org/freedesktop/DBus",
            "--method",
            "org.freedesktop.DBus.ListNames",
        ]
    )
    print(f"ListNames exit={code}")
    if code != 0:
        print(out)
        print("BLOCKED: session D-Bus unavailable")
        return

    portal = "org.freedesktop.portal.Desktop" in out
    print(f"session_bus_portal_present={portal}")
    for name in sorted({line.strip("' ,") for line in out.split("'") if "portal" in line.lower()}):
        if name:
            print(f"  bus_name={name}")

    code, xml = _run(
        [
            "gdbus",
            "introspect",
            "--session",
            "--dest",
            "org.freedesktop.portal.Desktop",
            "--object-path",
            "/org/freedesktop/portal/desktop",
            "--recurse",
        ]
    )
    print(f"portal introspect exit={code}")
    if code == 0:
        for iface in ("RemoteDesktop", "InputCapture", "ScreenCast", "Screenshot"):
            print(f"  introspect_has_{iface}={iface in xml}")
    else:
        print(xml)

    print("\nAttempting RemoteDesktop CreateSession (expect denial or NotSupported on X11):")
    code, resp = _run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.portal.Desktop",
            "--object-path",
            "/org/freedesktop/portal/desktop",
            "--method",
            "org.freedesktop.portal.RemoteDesktop.CreateSession",
            "{}",
        ]
    )
    print(f"CreateSession exit={code}")
    print(resp)


def _evdev_probe() -> None:
    print("\n=== E3b evdev probe ===")
    uid = os.getuid()
    user = pwd.getpwuid(uid).pw_name
    groups = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
    print(f"user={user} uid={uid} groups={groups}")
    print(f"in_input_group={'input' in groups}")

    dev_input = Path("/dev/input")
    event_nodes = sorted(dev_input.glob("event*")) if dev_input.is_dir() else []
    print(f"event_nodes={len(event_nodes)}")
    for node in event_nodes[:5]:
        print(f"  {node}")

    # Open test without adding evdev pip dep — use readable check
    readable = sum(1 for n in event_nodes[:5] if os.access(n, os.R_OK))
    print(f"readable_event_nodes_among_first_5={readable}")
    if readable == 0:
        print("evdev capture would require input group membership or root")


def main() -> int:
    print("=== E3 Wayland input capture ===")
    print(f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '')!r}")
    print(f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '')!r}")
    print(f"DISPLAY={os.environ.get('DISPLAY', '')!r}")

    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "x11" and not os.environ.get(
        "WAYLAND_DISPLAY"
    ):
        print(
            "NOTE: native Wayland session not available — E3a permission-dialog UX and "
            "E3b compositor-relative pointer semantics cannot be validated on this login."
        )

    _portal_probe()
    _evdev_probe()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
