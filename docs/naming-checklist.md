# Naming checklist (human checkpoint)

**Decision recorded 2026-07-13.** The product is **Ordine** (Italian/Latin for order; from the ordinal guarantee at the product's core). The mechanical rename landed in commit `chore: rename product to Ordine (naming checklist executed)` — pre-release, no migration shims.

## 1. Availability checks (performed)

| Surface | Check | Result |
|---------|-------|--------|
| PyPI | https://pypi.org/project/conveyor/ | Taken (unrelated) |
| PyPI (target) | https://pypi.org/project/ordine/ | Chosen distribution name: `ordine` |
| GitHub | org/repo name | May differ from PyPI |
| Debian package | `apt-cache search ordine` | Chosen deb `-n`: `ordine` |
| General search | web + trademark skim | Record kept in issue/PR thread |

**Recorded names:** brand **Ordine**, PyPI **`ordine`**, deb **`ordine`**, import package **`ordine`**, CLI **`ordine`**.

## 2. Mechanical rename list (executed)

| Item | Was | Now |
|------|-----|-----|
| `pyproject.toml` `[project].name` | `conveyor-automation` | `ordine` |
| Import package | `conveyor` | `ordine` (`src/ordine`) |
| CLI binary | `conveyor` | `ordine` |
| Entry-point groups | `conveyor.steps` / `conveyor.engines` | `ordine.steps` / `ordine.engines` |
| Config dir | `~/.config/conveyor` | `~/.config/ordine` |
| Data dir | `~/.local/share/conveyor` | `~/.local/share/ordine` |
| LLM env (openai_compatible) | `CONVEYOR_LLM_API_KEY` | `ORDINE_LLM_API_KEY` |
| Config env | `CONVEYOR_CONFIG` | `ORDINE_CONFIG` |
| Keyring service | `conveyor` | `ordine` |
| Deb install root | `/opt/conveyor` | `/opt/ordine` |
| systemd unit | `conveyor.service` | `ordine.service` |
| Tagline | (various) | Ordine — self-healing task pipelines for your desktop. |

## 3. What stayed `conveyor` (intentional)

| Item | Rationale |
|------|-----------|
| Historical `CHANGELOG.md` bullets | Audit trail of development under the old name |
| This checklist §1 availability table | Records checks against the old PyPI name |
| Parent-repo plan paths `Plans/conveyor-step-NN-plan.md` | Filenames in the parent monorepo (not renamed) |

## 4. Sign-off

- [x] Human decision recorded (2026-07-13 — product name **Ordine**)
- [x] Rename commit merged (pre-0.1.0 tag)
- [ ] `scripts/bump_version.py 0.1.0` and CHANGELOG cut (after CI green on main)
