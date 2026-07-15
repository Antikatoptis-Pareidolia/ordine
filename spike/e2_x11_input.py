#!/usr/bin/env python3
"""E2: Global input capture on X11 via pynput (5s listen, reports events)."""

from __future__ import annotations

import os
import time
from threading import Event, Thread

from pynput import keyboard, mouse
from pynput.keyboard import Controller as KeyController
from pynput.mouse import Controller as MouseController


def main() -> int:
    if not os.environ.get("DISPLAY"):
        print("BLOCKED: DISPLAY is unset — cannot test X11 pynput capture")
        return 2

    stop = Event()
    deadline = time.monotonic() + 5.0
    clicks: list[str] = []
    keys: list[str] = []

    def on_click(x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if pressed:
            clicks.append(f"click x={x} y={y} button={button.name} ts={time.monotonic():.3f}")
        if time.monotonic() >= deadline:
            stop.set()
            return False

    def on_press(key: keyboard.Key | keyboard.KeyCode) -> None:
        label = getattr(key, "char", None) or getattr(key, "name", str(key))
        keys.append(f"key {label!r} ts={time.monotonic():.3f}")
        if time.monotonic() >= deadline:
            stop.set()
            return False

    print("=== E2 pynput global capture (5 seconds) ===")
    print(f"DISPLAY={os.environ.get('DISPLAY')!r}")
    print("Interact with another window now (click + type)...")
    print("(script also injects one synthetic click+key after 1s for unattended runs)")

    def _inject() -> None:
        time.sleep(1.0)
        mc = MouseController()
        kc = KeyController()
        mc.click(mouse.Button.left, 1)
        kc.press("a")
        kc.release("a")

    Thread(target=_inject, daemon=True).start()

    t0 = time.monotonic()
    with mouse.Listener(on_click=on_click) as ml, keyboard.Listener(on_press=on_press) as kl:
        while time.monotonic() < deadline and not stop.is_set():
            time.sleep(0.05)
        ml.stop()
        kl.stop()

    elapsed = time.monotonic() - t0
    print(f"\nelapsed_s={elapsed:.2f}")
    print(f"clicks_captured={len(clicks)}")
    for line in clicks:
        print(line)
    print(f"keys_captured={len(keys)}")
    for line in keys[:20]:
        print(line)
    if len(keys) > 20:
        print(f"... ({len(keys) - 20} more keys omitted)")

    if not clicks and not keys:
        print("FAIL: zero events captured (no user input during window or capture blocked)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
