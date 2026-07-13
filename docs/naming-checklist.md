# Naming checklist (human checkpoint)

**Do not rename anything in code until this checklist is completed and a human records the decision.** The 0.1.0 CHANGELOG cut is blocked on this step.

## 1. Availability checks to perform

| Surface | Check | Notes |
|---------|-------|-------|
| PyPI | https://pypi.org/project/conveyor/ | Almost certainly taken |
| PyPI (current) | https://pypi.org/project/conveyor-automation/ | Distribution name in `pyproject.toml` today |
| GitHub | org/repo name | May differ from PyPI |
| Debian package | `apt-cache search conveyor` | `conveyor` vs `conveyor-automation` |
| General search | web + trademark skim | Avoid collision with unrelated "Conveyor" products |

Record chosen **brand name**, **PyPI distribution name**, and **deb `-n` name**.

## 2. Mechanical rename list (after decision)

Apply in a dedicated rename commit with **no behavior changes**.

| Item | Current | Candidate field |
|------|---------|-----------------|
| `pyproject.toml` `[project].name` | `conveyor-automation` | PyPI distribution |
| README title, badges, URLs | `Conveyor` / `OWNER` placeholder | brand strings |
| `fpm -n` in `scripts/build_deb.sh` | `conveyor` | deb binary package name |
| systemd unit / description strings | `Conveyor` | user-visible |
| GitHub repo name | (current) | if moving |
| docs strings | `Conveyor` | prose only |

## 3. What may stay `conveyor`

These are **optional** to rename even if the public brand changes:

| Item | Rationale |
|------|-----------|
| Import package `conveyor` | Breaking for plugins and docs |
| CLI binary `conveyor` | Muscle memory, systemd `ExecStart` |
| Config dir `~/.config/conveyor` | User data migration cost |
| SQLite / data paths under `.../conveyor/` | Same |

If the brand diverges (e.g. "PipelineForge" marketing with `conveyor` CLI), document the mapping in README.

## 4. Sign-off

- [ ] Human decision recorded (issue or PR comment)
- [ ] Rename commit merged (if any)
- [ ] `scripts/bump_version.py 0.1.0` and CHANGELOG cut unblocked
