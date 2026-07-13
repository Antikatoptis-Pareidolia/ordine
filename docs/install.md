# Installation

## Requirements

- Linux (primary target; developed on Ubuntu 24.04)
- Python ≥ 3.11
- **ImageMagick** (`imagemagick` package) — recommended for production image pipelines; Pillow fallback exists for some steps

## pipx (recommended)

```bash
pipx install conveyor-automation
pipx ensurepath
conveyor --help
```

Upgrade: `pipx upgrade conveyor-automation`

## `.deb` (self-contained venv)

Build locally (requires [fpm](https://fpm.readthedocs.io/)):

```bash
bash scripts/build_deb.sh
sudo apt install ./dist/conveyor_*_amd64.deb imagemagick
```

The package installs a venv under `/opt/conveyor`, a `/usr/bin/conveyor` symlink, and a systemd user unit at `/usr/lib/systemd/user/conveyor.service`.

## From source (development)

```bash
sudo apt install imagemagick
git clone <repo> && cd conveyor
uv venv && uv sync --extra dev
uv pip install -e tests/fixtures/conveyor_test_plugin
uv run conveyor --help
```

## First run

```bash
conveyor init                    # writes ~/.config/conveyor/config.toml
conveyor example ~/conveyor-demo
cd ~/conveyor-demo && conveyor run png-cleanup.yml --oneshot
```

## systemd (user service)

**Deb install** — unit is pre-installed:

```bash
systemctl --user daemon-reload
systemctl --user enable --now conveyor
systemctl --user status conveyor
```

**pipx install** — copy or adapt `packaging/conveyor.service`, setting:

```ini
ExecStart=%h/.local/bin/conveyor serve
```

Logs: `journalctl --user -u conveyor -f`

Optional retention at startup: set `on_serve_start = true` under `[retention]` in config.

## Dependency notes

- **httpx** is a single runtime dependency (also used by Starlette's test client in dev); it is not duplicated in `[dev]`.
- **typer** (full package) is kept over `typer-slim` because the CLI uses subcommand groups (`llm`, etc.); slim would not reduce installed surface meaningfully.

## Future packaging (not in 0.1)

Windows/macOS installers, Flatpak/Snap/AppImage, and hosted docs site are out of scope for 0.1. Track via GitHub issues if needed.
