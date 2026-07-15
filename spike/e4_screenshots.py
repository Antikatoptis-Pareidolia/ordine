#!/usr/bin/env python3
"""E4: Screenshot capture — X11 via mss; Wayland portal via gdbus."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent / "output" / "e4"
OUT.mkdir(parents=True, exist_ok=True)


def _run(cmd: list[str]) -> tuple[int, str]:
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def _x11_mss() -> int:
    print("=== E4 X11 mss ===")
    if not os.environ.get("DISPLAY"):
        print("BLOCKED: DISPLAY unset")
        return 2

    import mss
    import mss.tools

    with mss.mss() as sct:
        monitors = sct.monitors
        print(f"monitor_count={len(monitors) - 1}")
        for idx, mon in enumerate(monitors):
            print(f"  monitor[{idx}]={mon}")

        times: list[float] = []
        for i in range(5):
            t0 = time.perf_counter()
            shot = sct.grab(monitors[1])
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
            path = OUT / f"x11_mss_{i:02d}.png"
            mss.tools.to_png(shot.rgb, shot.size, output=str(path))
            print(f"  shot {i}: {elapsed * 1000:.1f}ms -> {path.name} {path.stat().st_size} bytes")

    avg = sum(times) / len(times)
    print(f"avg_grab_ms={avg * 1000:.1f}")
    print(f"max_grab_ms={max(times) * 1000:.1f}")
    return 0


def _wayland_portal() -> None:
    print("\n=== E4 Wayland portal Screenshot ===")
    if os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
        print(
            "BLOCKED: not a native Wayland session — cannot measure KDE portal prompt UX "
            "(one-shot vs per-session) without logging into Wayland."
        )
        return

    for i in range(2):
        t0 = time.perf_counter()
        code, out = _run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.portal.Desktop",
                "--object-path",
                "/org/freedesktop/portal/desktop",
                "--method",
                "org.freedesktop.portal.Screenshot.Screenshot",
                "",
                "{}",
            ]
        )
        elapsed = time.perf_counter() - t0
        print(f"  portal shot {i}: exit={code} elapsed_ms={elapsed * 1000:.0f}")
        print(out)
        time.sleep(2)


def main() -> int:
    print("=== E4 screenshots ===")
    code = _x11_mss()
    _wayland_portal()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
