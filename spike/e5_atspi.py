#!/usr/bin/env python3
"""E5: AT-SPI semantic events while driving GIMP and Dolphin."""

from __future__ import annotations

import os
import subprocess
import sys
import time


def _require_pyatspi() -> object:
    try:
        import pyatspi  # type: ignore[import-untyped]
    except ImportError as exc:
        print(f"BLOCKED: python3-pyatspi not importable: {exc}")
        print("Install system package: sudo apt install python3-pyatspi")
        sys.exit(2)
    return pyatspi


def _collect_events(pyatspi: object, seconds: float) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []

    def listener(event: object) -> None:
        try:
            source = event.source  # type: ignore[attr-defined]
            app = source.getApplication()  # type: ignore[union-attr]
            events.append(
                {
                    "ts": f"{time.monotonic():.3f}",
                    "type": str(getattr(event, "type", "")),
                    "app": str(getattr(app, "name", "")),
                    "role": str(getattr(source, "roleName", "")),
                    "name": str(getattr(source, "name", "")),
                }
            )
        except Exception as exc:
            events.append({"ts": f"{time.monotonic():.3f}", "error": str(exc)})

    pyatspi.Registry.registerEventListener(listener, "object:state-changed:focused")
    pyatspi.Registry.registerEventListener(listener, "object:selection-changed")
    pyatspi.Registry.registerEventListener(listener, "object:property-change:accessible-name")
    time.sleep(seconds)
    pyatspi.Registry.deregisterEventListener(listener)
    return events


def _launch(cmd: list[str], wait: float = 2.0) -> subprocess.Popen[str] | None:
    print(f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print(f"BLOCKED: executable missing: {cmd[0]}")
        return None
    time.sleep(wait)
    return proc


def _dump_desktop_tree(pyatspi: object, app_hint: str, limit: int = 15) -> None:
    desktop = pyatspi.Registry.getDesktop(0)
    shown = 0
    for app in desktop:
        name = str(getattr(app, "name", ""))
        if app_hint.lower() not in name.lower():
            continue
        print(f"\n--- tree for app {name!r} (first {limit} nodes) ---")
        for node in _walk(app):
            role = str(getattr(node, "roleName", ""))
            n = str(getattr(node, "name", ""))
            if not n and role not in {"menu", "menu item", "push button", "tool bar"}:
                continue
            print(f"  role={role!r} name={n!r}")
            shown += 1
            if shown >= limit:
                break
        break


def _walk(root: object):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        try:
            for child in node:  # type: ignore[union-attr]
                stack.append(child)
        except Exception:
            continue


def main() -> int:
    if not os.environ.get("DISPLAY"):
        print("BLOCKED: DISPLAY unset")
        return 2

    pyatspi = _require_pyatspi()
    print("=== E5 AT-SPI semantics ===")
    print(f"DISPLAY={os.environ.get('DISPLAY')!r}")

    gimp = _launch(["gimp", "-b", '(python-fu-eval 0 "1")', "-b", "(gimp-quit 0)"], wait=0.5)
    # Launch GIMP normally for AT-SPI (batch mode exits too fast); use empty doc
    if gimp is not None:
        gimp.terminate()
    gimp = _launch(["gimp"], wait=4.0)

    print("\n--- GIMP: 8s event listen (focus menus manually if idle) ---")
    gimp_events = _collect_events(pyatspi, 8.0)
    print(f"gimp_events={len(gimp_events)}")
    for ev in gimp_events[:25]:
        print(ev)
    if len(gimp_events) > 25:
        print(f"... ({len(gimp_events) - 25} more)")

    _dump_desktop_tree(pyatspi, "gimp")

    if gimp is not None:
        gimp.terminate()
        try:
            gimp.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gimp.kill()

    dolphin = _launch(["dolphin"], wait=3.0)
    print("\n--- Dolphin: 6s event listen ---")
    dolphin_events = _collect_events(pyatspi, 6.0)
    print(f"dolphin_events={len(dolphin_events)}")
    for ev in dolphin_events[:25]:
        print(ev)
    if len(dolphin_events) > 25:
        print(f"... ({len(dolphin_events) - 25} more)")

    _dump_desktop_tree(pyatspi, "dolphin")

    if dolphin is not None:
        dolphin.terminate()
        try:
            dolphin.wait(timeout=5)
        except subprocess.TimeoutExpired:
            dolphin.kill()

    usable = sum(
        1
        for ev in gimp_events + dolphin_events
        if ev.get("role") and ev.get("name") and "error" not in ev
    )
    print(f"\nusable_named_events={usable}")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
