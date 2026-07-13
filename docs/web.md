# Ordine Web UI

Local-first web interface for operating pipelines without the CLI. Built with FastAPI, Jinja2, and a **single vendored HTMX 2.0.4** file (`static/htmx.min.js` — see `static/htmx.version`). No Node.js, no CDN, no frontend build step.

## Starting the server

```bash
ordine serve              # binds 127.0.0.1:8484 by default
ordine serve --host 0.0.0.0 --port 9000   # prints a loud warning (no auth in v1)
```

Configuration comes from the same TOML as the CLI (`[web]` section: `host`, `port`, `autostart_pipelines`).

## Pages

| URL | Purpose |
|-----|---------|
| `/` | Dashboard — pipeline cards, counts, start/pause, register-playbook form |
| `/partials/pipelines` | HTMX fragment polled every 2s (degrades to static cards with JS off) |
| `/pipelines/{id}/tasks` | Task table with status filter tabs, 50 per page |
| `/tasks/{id}` | Task detail — metadata, step timeline from `task.json`, log tails, before/after images, retry/cancel |
| `/flags` | Open flags inbox (level desc, then age) with resolve form |
| `/settings` | View paths (read-only); edit `[runner]` + `[web]`; LLM placeholder heading only |

## JavaScript-free baseline

Every action uses real `<form method="post">` or `<a href>` elements. With JavaScript disabled, forms submit normally and the browser follows redirects. HTMX only enhances: polling refresh and inline swaps when `htmx.min.js` loads.

## Actions (POST)

| Route | Effect |
|-------|--------|
| `POST /pipelines` | Paste YAML → validate → register |
| `POST /pipelines/{id}/start` | ServiceManager start (current version) |
| `POST /pipelines/{id}/pause` | Graceful pause |
| `POST /tasks/{id}/retry` | `transition(pending)` when legal |
| `POST /tasks/{id}/cancel` | `transition(skipped)` when legal |
| `POST /flags/{id}/resolve` | Resolve flag (note required) |
| `POST /settings` | Atomic TOML write-back for runner + web |

Illegal transitions redirect back with a flash message — never HTTP 500.

## Security posture (v1)

- **Bind address:** `127.0.0.1` default. Non-local `--host` prints a warning; there is no authentication.
- **POST hardening:** Each POST must either carry `HX-Request: true` (HTMX) or include a same-origin `Origin`/`Referer`. Requests with a foreign origin, or with no HX-Request and no Origin/Referer (e.g. bare `curl`), receive **403**.
- **Artifacts:** `GET /artifacts/{task_id}/{rel_path}` resolves `(workdir / rel_path)` and rejects paths that escape the task workdir (404). Some malformed paths (e.g. a leading slash) are rejected by FastAPI routing with **422** before the handler runs — defense-in-depth; no file is served either way. Images served inline; `log.txt` as `text/plain`; other files as download.
- **Templates:** Jinja2 autoescape enabled; user content (YAML, messages, filenames) is never marked safe.
- **Future:** Step 15 notes — real CSRF tokens + optional auth (`hardening`).

## Screenshots

<!-- screenshot: dashboard with pipeline cards -->
<!-- screenshot: task detail with before/after images -->
<!-- screenshot: flags inbox -->

## Performance notes

Thumbnails are not generated; original images are served from the workdir. A caching/thumbnail follow-up may land in a later step.

## Manual walkthrough (no CLI)

1. `ordine serve`
2. Open `http://127.0.0.1:8484`
3. Paste flagship YAML → Register
4. Start pipeline → drop PNGs into the watched folder
5. Watch dashboard counters update (or refresh manually with JS off)
6. Open a task → inspect step logs and images
7. Resolve a flag from the Flags page
