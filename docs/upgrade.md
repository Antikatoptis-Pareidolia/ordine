# Upgrading Ordine

## Before you upgrade

1. **Stop** running services: `systemctl --user stop ordine` (or Ctrl-C on `ordine serve` / `ordine run`).
2. **Back up** the SQLite ledger and workdirs (defaults under XDG data):

```bash
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/ordine"
cp -a "$DATA/ordine.sqlite3" "$DATA/ordine.sqlite3.bak-$(date +%Y%m%d)"
cp -a "$DATA/workdirs" "$DATA/workdirs.bak-$(date +%Y%m%d)"
```

Also back up `~/.config/ordine/config.toml` (and `.env` if you use plaintext keys).

## pipx

```bash
pipx upgrade ordine
ordine --version
```

## `.deb`

```bash
# build or download the new package, then:
sudo apt install ./deb-dist/ordine_*_amd64.deb
ordine --version
```

Restart the user unit after packaging upgrades:

```bash
systemctl --user daemon-reload
systemctl --user restart ordine
```

## SCHEMA_VERSION mismatch

Ordine stores a SQLite `user_version` (`SCHEMA_VERSION` in `src/ordine/core/db.py`). On open, the ledger **hard-fails** if the on-disk version is not `0` (fresh) or the version this build expects.

Symptoms:

- CLI/web refuse to start with an error mentioning schema / `user_version`
- No automatic migration is applied in 0.2.x

What to do:

1. Confirm you restored a backup taken **before** the upgrade if you need the old data with the old binary.
2. Prefer upgrading only forward within the supported `0.2.x` line until a release ships an explicit migration (tracked for 0.3.0).
3. If you intentionally discard local state: stop Ordine, move `ordine.sqlite3` aside, and let the new version create a fresh DB (re-register playbooks afterward).

## Config notes (0.2.3+)

`[web]` splits **bind** (listen address) from **allowed_hosts** (HTTP Host allowlist). Legacy `web.host` is still accepted as the bind address; wildcards (`0.0.0.0` / `::`) are never treated as Host allowlist entries. Bind/port changes require restarting `ordine serve`.

See [install.md](install.md) and [security.md](security.md).
