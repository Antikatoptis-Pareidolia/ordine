# Installation

## Requirements

- Linux (primary target; developed on Ubuntu 24.04)
- Python ≥ 3.11
- **ImageMagick** (`imagemagick` package) — recommended for production image pipelines; Pillow fallback exists for some steps

## pipx (recommended)

```bash
pipx install ordine
pipx ensurepath
ordine --help
```

Upgrade: `pipx upgrade ordine`

## `.deb` (self-contained venv)

Build locally (requires [fpm](https://fpm.readthedocs.io/)):

```bash
bash scripts/build_deb.sh
sudo apt install ./deb-dist/ordine_*_amd64.deb imagemagick
```

The package installs a venv under `/opt/ordine`, a `/usr/bin/ordine` symlink, and a systemd user unit at `/usr/lib/systemd/user/ordine.service`.

## From source (development)

```bash
sudo apt install imagemagick
git clone https://github.com/Antikatoptis-Pareidolia/ordine.git && cd ordine
uv venv && uv sync --locked --extra dev
uv pip install -e tests/fixtures/ordine_test_plugin
uv run ordine --help
```

## First run

```bash
ordine init                    # writes ~/.config/ordine/config.toml
ordine example ~/ordine-demo
cd ~/ordine-demo && ordine run png-cleanup.yml --oneshot
```

## systemd (user service)

**Deb install** — unit is pre-installed:

```bash
systemctl --user daemon-reload
systemctl --user enable --now ordine
systemctl --user status ordine
```

**pipx install** — copy or adapt `packaging/ordine.service`, setting:

```ini
ExecStart=%h/.local/bin/ordine serve
```

Logs: `journalctl --user -u ordine -f`

Optional retention at startup: set `on_serve_start = true` under `[retention]` in config.

## Dependency notes

- **httpx** is a single runtime dependency (also used by Starlette's test client in dev); it is not duplicated in `[dev]`.
- **typer** (full package) is kept over `typer-slim` because the CLI uses subcommand groups (`llm`, etc.); slim would not reduce installed surface meaningfully.

## Future packaging (not in 0.1)

Windows/macOS installers, Flatpak/Snap/AppImage, and hosted docs site are out of scope for 0.1. Track via GitHub issues if needed.
