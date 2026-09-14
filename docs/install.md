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

Upgrade: `pipx upgrade ordine` — see [upgrade.md](upgrade.md) for backup and schema notes.

## `.deb` (isolated venv)

Build locally (requires [uv](https://docs.astral.sh/uv/) and
[fpm](https://fpm.readthedocs.io/), release-tested at `1.17.0`):

```bash
bash scripts/build_deb.sh
sudo apt install ./deb-dist/ordine_*_amd64.deb imagemagick
```

The package installs a venv under `/opt/ordine`, a `/usr/bin/ordine` symlink, and a systemd user unit at `/usr/lib/systemd/user/ordine.service`.
Release builds install the already-tested wheel with the exact runtime versions exported from
`uv.lock`; the `.deb` does not resolve a fresh dependency set during packaging. The package
declares the same Python minor version used to build its compiled wheels and uses that system
interpreter on the target.

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

The packaged unit assumes **localhost-first**: `ordine serve` binds `127.0.0.1` by default and there is no authentication. Non-loopback binds are **refused** unless you set `web.i_understand_no_auth = true` or pass `--i-understand-no-auth`. Do not expose the port without a reverse proxy and auth. Optional `Protect*` hardening can be added as a drop-in — see comments in `packaging/ordine.service`.
Only one `serve`/`run` writer may own a given database (advisory lock); `ordine status` remains read-only.


## Dependency notes

- **httpx** is the runtime HTTP client; development uses **httpx2** for Starlette's test transport.
- **typer** (full package) is kept over `typer-slim` because the CLI uses subcommand groups (`llm`, etc.); slim would not reduce installed surface meaningfully.

## Future packaging

Windows/macOS installers, Flatpak/Snap/AppImage, and a hosted docs site are not currently shipped. Track them via GitHub issues if needed.
