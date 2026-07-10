# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repo scaffold, tooling, CI (Step 1)
- Playbook schema, loader, JSON Schema export, and validation tests (Step 2)
- SQLite ledger with task state machine, branch attempts, flags, and name reservations (Step 3)
- Step contract, plugin registry, headless engine, and task workdirs (Step 4)
- Headless image steps (`image.validate`, `image.white_to_alpha`, `image.trim`, `image.export`) with ImageMagick/Pillow backends (Step 5)
- Folder watch and manual triggers with settle detection, startup rescan, and ledger sink (Step 6)
- `Ledger.create_task_arrival` — atomic arrival-order ordinal assignment + insert (planned Step 3 amendment, landed Step 6)
