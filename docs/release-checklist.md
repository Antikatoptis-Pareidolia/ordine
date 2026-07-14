# Release checklist

Use this for every `vX.Y.Z` tag. CI automates build/test/publish; the VM gate stays manual.

## Pre-release

- [ ] All changes on `main`; CI green
- [ ] `[Unreleased]` in `CHANGELOG.md` has bullets for this release
- [ ] **Name decision** complete — see [naming-checklist.md](naming-checklist.md) (blocks first `0.1.0` cut)
- [ ] `uv run pytest -m "not llm_live"` green locally
- [ ] Run the full local gate before pushing the release commit (`ruff check .`, `ruff format --check .`, `mypy`, `pytest -m "not llm_live"` with coverage floor)
- [ ] `bash scripts/build_deb.sh` succeeds; `ls deb-dist/*.deb`

## Version bump

```bash
uv run python scripts/bump_version.py X.Y.Z   # refuses empty Unreleased; refreshes uv.lock
git add pyproject.toml src/ordine/__init__.py CHANGELOG.md uv.lock
git commit -m "chore: release vX.Y.Z"
```

`test_version_sync.py` guards `pyproject.toml` ↔ `ordine.__version__`.

## Tag & CI

```bash
git tag vX.Y.Z
git push origin main --tags
```

`release.yml` on `v*`:

1. Full lint, mypy, pytest
2. Assert tag (without `v`) == package version
3. `uv build` → PyPI via OIDC trusted publishing (no token secrets)
4. `uv build` → PyPI (`dist/`); `build_deb.sh` → `deb-dist/`; attach wheel, sdist, `.deb` to GitHub Release
5. Changelog section extracted via `scripts/extract_changelog.py`

## Manual gates (not CI)

### Fresh Kubuntu VM — 10-minute test

Someone who did **not** build the project:

1. Start stopwatch
2. Follow **README quickstart only** (pipx or deb + imagemagick + `ordine example` + run + serve)
3. Stop when the web UI shows a completed pipeline
4. **Pass:** < 10 minutes

Record OS version and any friction in the release issue.

### Optional polish

- [ ] Record demo GIF from `demo/demo.tape` with [vhs](https://github.com/charmbracelet/vhs) (not in CI)
- [ ] `pipx install` on a clean user account; walk the UI
- [ ] `systemctl --user enable --now ordine`; verify survive re-login
- [ ] `retention.on_serve_start = true`; confirm log line on serve

## Announce

- [ ] GitHub Release notes reviewed
- [ ] PyPI page shows correct version
- [ ] README CI badge loads (Antikatoptis-Pareidolia/ordine)
