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

## Schema migrations (0.3+)

Ordine stores a SQLite `user_version` (`SCHEMA_VERSION` in `src/ordine/core/db.py` /
`src/ordine/core/migrations.py`). On open:

| On-disk version | Behavior |
|-----------------|----------|
| `0` (fresh) | `create_all`, apply post-bootstrap migrations, stamp latest |
| `1 … SCHEMA_VERSION-1` | Apply each N→N+1 migration in order, then stamp latest |
| `SCHEMA_VERSION` | No-op |
| `> SCHEMA_VERSION` | **Hard-fail** (`SchemaVersionError`) — this build cannot open newer DBs |

Phase 2 ships the migration **framework** plus a trivial `1→2` migration that creates
`ordine_schema_meta` (bookkeeping only). Future schema changes add a function to
`MIGRATIONS` and bump `SCHEMA_VERSION`.

Symptoms of a hard-fail:

- CLI/web refuse to start with an error mentioning schema / `user_version`

What to do:

1. Confirm you restored a backup taken **before** the upgrade if you need the old data with the old binary.
2. Prefer upgrading only forward within the supported line.
3. If you intentionally discard local state: stop Ordine, move `ordine.sqlite3` aside, and let the new version create a fresh DB (re-register playbooks afterward).

## Single-writer lock (S13)

`ordine serve` and `ordine run` take an exclusive flock on `{db}.lock` next to the ledger.
A second writer against the same DB exits non-zero with a clear message.
**`ordine status` does not take the write lock** — it may run while a writer is active and
reports worker heartbeats from sidecar files under `{db}.heartbeats/`.

## Config notes (0.2.3+ / 0.3)

`[web]` splits **bind** (listen address) from **allowed_hosts** (HTTP Host allowlist). Legacy `web.host` is still accepted as the bind address; wildcards (`0.0.0.0` / `::`) are never treated as Host allowlist entries. Bind/port changes require restarting `ordine serve`.

Non-loopback binds require `web.i_understand_no_auth = true` or CLI `--i-understand-no-auth` (S1).

See [install.md](install.md) and [security.md](security.md).
