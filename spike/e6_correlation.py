#!/usr/bin/env python3
"""E6: Correlate pynput clicks with AT-SPI semantic events (join window sweep)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from threading import Event

from pynput import mouse


@dataclass
class Click:
    ts: float
    x: int
    y: int


@dataclass
class Semantic:
    ts: float
    app: str
    role: str
    name: str


def main() -> int:
    if not os.environ.get("DISPLAY"):
        print("BLOCKED: DISPLAY unset")
        return 2

    try:
        import pyatspi  # type: ignore[import-untyped]
    except ImportError as exc:
        print(f"BLOCKED: python3-pyatspi not importable: {exc}")
        return 2

    clicks: list[Click] = []
    semantics: list[Semantic] = []
    stop = Event()
    deadline = time.monotonic() + 10.0

    def on_click(x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if pressed:
            clicks.append(Click(ts=time.monotonic(), x=x, y=y))
        if time.monotonic() >= deadline:
            stop.set()
            return False

    def listener(event: object) -> None:
        try:
            source = event.source  # type: ignore[attr-defined]
            app = source.getApplication()  # type: ignore[union-attr]
            semantics.append(
                Semantic(
                    ts=time.monotonic(),
                    app=str(getattr(app, "name", "")),
                    role=str(getattr(source, "roleName", "")),
                    name=str(getattr(source, "name", "")),
                )
            )
        except Exception:
            return

    pyatspi.Registry.registerEventListener(listener, "object:state-changed:focused")
    pyatspi.Registry.registerEventListener(listener, "object:selection-changed")

    print("=== E6 correlation (10s): click widgets in GIMP or Dolphin ===")
    with mouse.Listener(on_click=on_click) as ml:
        while time.monotonic() < deadline and not stop.is_set():
            time.sleep(0.05)
        ml.stop()

    pyatspi.Registry.deregisterEventListener(listener)

    print(f"clicks={len(clicks)} semantics={len(semantics)}")
    windows_ms = [50, 100, 200, 500, 1000, 2000]
    for window_ms in windows_ms:
        window_s = window_ms / 1000.0
        matched = 0
        for click in clicks:
            best = None
            best_dt = window_s
            for sem in semantics:
                if not sem.name:
                    continue
                dt = abs(sem.ts - click.ts)
                if dt <= window_s and dt <= best_dt:
                    best = sem
                    best_dt = dt
            if best is not None:
                matched += 1
        print(f"window_ms={window_ms} matched_clicks={matched}/{len(clicks)}")

    print("\n--- per-click best semantic match (200ms window) ---")
    for click in clicks:
        best = None
        best_dt = 0.2
        for sem in semantics:
            if not sem.name:
                continue
            dt = abs(sem.ts - click.ts)
            if dt <= best_dt and (best is None or dt < best_dt):
                best = sem
                best_dt = dt
        if best:
            print(
                f"click@({click.x},{click.y}) dt_ms={(best.ts - click.ts) * 1000:.0f} "
                f"app={best.app!r} role={best.role!r} name={best.name!r}"
            )
        else:
            print(f"click@({click.x},{click.y}) NO_MATCH within 200ms")

    if not clicks:
        print("FAIL: no clicks recorded during window")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
