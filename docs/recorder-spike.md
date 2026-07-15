# Recorder spike decision document (Step 16 Phase A)

**Branch:** `spike/step-16-recorder`
**Date:** 2026-07-15
**Dev machine:** Kubuntu / KDE Plasma, Linux 6.17, primary session **X11** (`kwin_x11`, Xorg on `:0`)
**Ordine version at spike:** 0.2.0 (408 tests green on `main` before branch)

This document records Phase A experiments E1–E6 only. **No MVP code** was written under `src/ordine/`.

## Spike dependencies (quarantined — not in `pyproject.toml`)

| Package | Used by | Justification |
|---------|---------|---------------|
| `pynput` | E2, E6 | Plan-specified global input capture test on X11 |
| `mss` | E4 | Plan-specified X11 screenshot rate test |

System packages **not** pip-installed (per plan):

| Package | Used by | Status on dev machine |
|---------|---------|------------------------|
| `python3-pyatspi` | E5, E6 | **Not installed** (`apt install` requires sudo; experiment BLOCKED) |

Spike scripts live in `spike/` (excluded from wheel/sdist via `pyproject.toml` `[tool.hatch.build.targets.sdist] exclude`).

---

## Context update: `shell.run` in catalog (post-plan)

The Step 16 plan compiler contract says drafts target the **headless step catalog**. Since v0.2.0, **`shell.run` is a shipped built-in step** (universal command execution).

**Question for human review (do not silently expand MVP scope):**

When the demonstrated GUI action has no `image.*` headless equivalent, should the compiler be allowed to propose `shell.run` (with explicit cmd) as a fallback, or must it still refuse and only map to existing non-shell catalog steps? This spike does not answer that product question; E5/E6 semantics data needed first.

---

## E1 — Session detection

**Command:**
```bash
cd /home/ai/Projects/4. Automation\ Program/conveyor
export DISPLAY=:0
python3 spike/e1_session.py
```

**Raw output:**
```
=== E1 session environment ===
XDG_SESSION_TYPE='x11'
WAYLAND_DISPLAY=''
DISPLAY=':0'
XDG_CURRENT_DESKTOP='KDE'
DESKTOP_SESSION='plasma'

=== derived session class ===
session_class=x11
platform=Linux-6.17.0-35-generic-x86_64-with-glibc2.39

=== loginctl session Type (if available) ===
exit=1
Failed to get path for session 'self': Caller does not belong to any known session.

=== KWin / compositor hints ===
$ pgrep -a kwin -> exit=0
2585 /usr/bin/kwin_x11 --replace
35614 /usr/bin/kwin_x11 --replace
$ pgrep -a Xorg -> exit=0
1632 /usr/lib/xorg/Xorg -nolisten tcp -background none -seat seat0 vt2 -auth /run/sddm/xauth_vmXzur -noreset -displayfd 16
35072 /usr/lib/xorg/Xorg -nolisten tcp -background none -seat seat0 vt1 -auth /run/sddm/xauth_ZUQdoa -noreset -displayfd 20
$ pgrep -a Xwayland -> exit=1
(no matches)
```

**Criteria (plan):** Print matrix of session env vars; identify X11 vs Wayland on dev machine.

**Verdict:** **PASS** — active session is **X11 / KDE Plasma**. Wayland login not available in this session (no `WAYLAND_DISPLAY`, no Xwayland). Plan asked to check both session types at login screen; **Wayland column remains untested until a separate Wayland login**.

**Surprises:** Two `kwin_x11` and two Xorg processes (likely SDDM + active seat); `loginctl show-session self` failed (SSH/automation context).

---

## E2 — Global input capture (X11 / pynput)

**Command:**
```bash
export DISPLAY=:0
spike/.venv/bin/python spike/e2_x11_input.py
```

**Raw output:**
```
=== E2 pynput global capture (5 seconds) ===
DISPLAY=':0'
Interact with another window now (click + type)...
(script also injects one synthetic click+key after 1s for unattended runs)

elapsed_s=5.03
clicks_captured=1
click x=1110 y=461 button=left ts=239158.853
keys_captured=1
key 'a' ts=239158.860
```

**Criteria:** Global clicks + keys captured while other apps focused; note latency/reliability.

**Verdict:** **PASS (minimal)** — pynput listener received injected click and key on `:0`. Script adds synthetic injection for unattended runs; manual multi-app focus test still recommended on review machine.

**Surprises:** Initial run with zero user input failed (0 events); injection thread added for reproducibility. `xdotool` not installed on dev machine.

---

## E3 — Global input capture (Wayland)

**Command:**
```bash
export DISPLAY=:0
python3 spike/e3_wayland_input.py
```

**Raw output:**
```
=== E3 Wayland input capture ===
XDG_SESSION_TYPE='x11'
WAYLAND_DISPLAY=''
DISPLAY=':0'
NOTE: native Wayland session not available — E3a permission-dialog UX and E3b compositor-relative pointer semantics cannot be validated on this login.
=== E3a xdg-desktop-portal D-Bus probe (gdbus) ===
ListNames exit=0
session_bus_portal_present=True
  bus_name=org.freedesktop.impl.portal.PermissionStore
  bus_name=org.freedesktop.impl.portal.desktop.gtk
  bus_name=org.freedesktop.impl.portal.desktop.kde
  bus_name=org.freedesktop.portal.Desktop
  bus_name=org.freedesktop.portal.Documents
  bus_name=org.freedesktop.portal.Fcitx
  bus_name=org.freedesktop.portal.IBus
portal introspect exit=0
  introspect_has_RemoteDesktop=True
  introspect_has_InputCapture=False
  introspect_has_ScreenCast=True
  introspect_has_Screenshot=True

Attempting RemoteDesktop CreateSession (expect denial or NotSupported on X11):
CreateSession exit=1
Error: GDBus.Error:org.gtk.GDBus.UnmappedGError.Quark._g_2dio_2derror_2dquark.Code0: Missing token

=== E3b evdev probe ===
user=ai uid=1000 groups=['adm', 'cdrom', 'sudo', 'dip', 'plugdev', 'lpadmin', 'sambashare', 'shared', 'docker']
in_input_group=False
event_nodes=22
  /dev/input/event0
  /dev/input/event1
  /dev/input/event10
  /dev/input/event11
  /dev/input/event12
readable_event_nodes_among_first_5=0
evdev capture would require input group membership or root
```

**Criteria:** (a) Portal RemoteDesktop/InputCapture availability + permission UX on KDE Wayland; (b) evdev with `input` group.

**Verdict:** **BLOCKED (Wayland UX)** / **PARTIAL (D-Bus introspection only)**

- **E3a:** Portal present on session bus; `RemoteDesktop` interface exists but **`InputCapture` absent** in introspection on this KDE build. `CreateSession` failed (`Missing token`) — expected without a Wayland session and portal flow. **Cannot observe permission-dialog UX without Wayland login.**
- **E3b:** User not in `input` group; **0/5** event nodes readable. evdev path not validated.

**Surprises:** `InputCapture` missing from portal introspection (may affect future compositor capture plans).

---

## E4 — Screenshots

**Command:**
```bash
export DISPLAY=:0
spike/.venv/bin/python spike/e4_screenshots.py
```

**Raw output:**
```
=== E4 screenshots ===
=== E4 X11 mss ===
monitor_count=1
  monitor[0]={'left': 0, 'top': 0, 'width': 1920, 'height': 1200}
  monitor[1]={'left': 0, 'top': 0, 'width': 1920, 'height': 1200, 'is_primary': True, 'output': 'eDP', 'name': 'Color LCD', 'unique_id': 'model=APPA02F&mfr_date=2015W04'}
  shot 0: 14.8ms -> x11_mss_00.png 506049 bytes
  shot 1: 8.5ms -> x11_mss_01.png 506200 bytes
  shot 2: 5.5ms -> x11_mss_02.png 509485 bytes
  shot 3: 5.0ms -> x11_mss_03.png 509481 bytes
  shot 4: 8.5ms -> x11_mss_04.png 511522 bytes
avg_grab_ms=8.4
max_grab_ms=14.8

=== E4 Wayland portal Screenshot ===
BLOCKED: not a native Wayland session — cannot measure KDE portal prompt UX (one-shot vs per-session) without logging into Wayland.
```

**Criteria:** X11 mss rate + multi-monitor; Wayland portal screenshot prompt behavior.

**Verdict:** **PASS (X11)** / **BLOCKED (Wayland portal UX)**

- X11: ~5–15 ms per full-screen grab on 1920×1200 — **sustainable for per-click shots**.
- Wayland: not tested (session is X11).

**Surprises:** `mss.mss()` deprecation warning (use `mss.MSS` in MVP backend).

---

## E5 — AT-SPI semantics (GIMP + Dolphin)

**Command:**
```bash
export DISPLAY=:0
python3 spike/e5_atspi.py
```

**Raw output:**
```
BLOCKED: python3-pyatspi not importable: No module named 'pyatspi'
Install system package: sudo apt install python3-pyatspi
```

**Criteria:** Event listeners on GIMP + Dolphin; usable role/name/app paths.

**Verdict:** **BLOCKED** — `python3-pyatspi` not installed; `sudo apt install` required. GIMP (`/usr/bin/gimp`) and Dolphin (`/usr/bin/dolphin`) are present but untested.

**Surprises:** None beyond missing system package.

---

## E6 — Click ↔ semantic correlation

**Command:**
```bash
export DISPLAY=:0
spike/.venv/bin/python spike/e6_correlation.py
```

**Raw output:**
```
BLOCKED: python3-pyatspi not importable: No module named 'pyatspi'
```

**Criteria:** Join window (ms) that reliably names clicked widget.

**Verdict:** **BLOCKED** — depends on E5.

---

## Support tier table (evidence-based)

| Tier | Session | Global input | Screenshots | AT-SPI semantics | Click↔semantic join | Evidence |
|------|---------|--------------|-------------|------------------|---------------------|----------|
| **A (full)** | X11 + KDE | **Likely yes** (E2 pass) | **Yes** (E4 pass) | **Unknown** (E5 blocked) | **Unknown** (E6 blocked) | E1–E2, E4 |
| **B (Wayland)** | Wayland + KDE | **Unknown** (E3 blocked) | **Unknown** (E4 blocked) | **Unknown** | **Unknown** | Not tested — need Wayland login |
| **C (evdev fallback)** | Any | **Partial** — raw events only, not in `input` group (E3b) | N/A | N/A | N/A | E3b |

**MVP backend recommendation from available evidence:** Implement **X11CaptureBackend** first using **pynput + mss**; treat AT-SPI as **required follow-up spike rerun** after `sudo apt install python3-pyatspi` before trusting compiler semantics. **Do not ship Wayland tier** until E3/E4 Wayland columns are filled on a native Wayland session.

---

## Recommendation

### **Proceed with amendments**

**Proceed** with Phase B MVP **only after**:

1. **Re-run E5 + E6** on this machine after `sudo apt install python3-pyatspi` (and manual GIMP menu navigation during listen window). Without this, the compiler cannot be validated against real semantic events — core thesis of Step 16.
2. **Schedule a Wayland KDE login session** to complete E3a UX, E4 portal screenshot prompt behavior, and revise the tier table.
3. **Human decision** on `shell.run` in compiler output when headless mapping fails (see Context update above).
4. MVP implements **top tier (X11)** per plan; document Wayland as **degraded / unsupported** until tier B evidence exists.

**Do not proceed** if product requires Wayland-first capture on day one — current evidence is insufficient.

**Evidence trail:**

| Claim | Backing |
|-------|---------|
| X11 session confirmed | E1 raw output |
| pynput captures global input on X11 | E2 raw output |
| mss fast enough for per-click shots | E4 avg 8.4 ms |
| Portal interfaces exist on KDE (D-Bus) | E3 introspection |
| AT-SPI viability unknown | E5 blocked |
| Join window unknown | E6 blocked |
| Wayland capture unknown | E3/E4 Wayland sections blocked |

---

## Gate status (branch)

Full repo gate (`ruff`, `mypy`, `pytest -m "not llm_live"`, core coverage floor) must pass with spike quarantined — verified at end of spike branch work.
