# Headless image steps

Four steps live in `conveyor.executors.headless.steps`. Each runs in isolation under the Step 4 contract; sequencing, retries, branches, and flag wiring are handled by the runner (Step 7).

## Steps overview

| Step ID | Purpose |
|---------|---------|
| `image.validate` | Corruption gate — corrupt/unreadable input yields `skip` with `flag_kind="corrupt_input"` |
| `image.white_to_alpha` | Make near-white pixels transparent; output is always PNG |
| `image.trim` | Crop to non-transparent bounds; optional transparent border padding |
| `image.export` | Atomic write into a user destination directory |

Source originals are never modified. All intermediate artifacts stay in the task workdir except `image.export`, which writes to the param-declared `dest`.

## Backend selection

Processing steps (`white_to_alpha`, `trim`) accept:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `backend` | `"auto"` \| `"imagemagick"` \| `"pillow"` | `"auto"` | Backend to use |
| `timeout_seconds` | `float` (> 0) | `60.0` | ImageMagick subprocess timeout |

- **`auto`**: ImageMagick when `magick` (IM7) or `convert` (IM6) is on `PATH`, otherwise Pillow.
- **`imagemagick`**: Requires ImageMagick. If absent, returns `fail` with message `"imagemagick not installed"` (recoverable — a Pillow recovery branch can catch it).
- **`pillow`**: Pure-Python fallback via Pillow.

`image.validate` and `image.export` always use Pillow for inspection/conversion; they do not expose a backend param.

### Fuzz approximation (`image.white_to_alpha`)

ImageMagick `-fuzz` measures RGB distance from the target color. The Pillow backend uses a per-channel threshold: `t = round(255 * (1 - fuzz/100))` — pixels with `r,g,b` all ≥ `t` become fully transparent.

The two backends approximate each other but are not pixel-perfect. Cross-backend tests allow ≥97% transparent/opaque agreement and opaque-pixel RGB differences ≤12. Pixel-perfect parity is a non-goal.

## Step parameters

### `image.validate`

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `formats` | `list[str]` | `["png"]` | Allowed formats (lowercase extensions/names) |

Checks (first failure → `skip` + `corrupt_input`):

1. `input_path` set, exists, size > 0
2. Pillow `Image.open` + `verify()` succeeds
3. Detected format is in `formats`

On success: `ok` with `output_path=None`. This step never returns `fail`.

### `image.white_to_alpha`

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `backend` | see above | `"auto"` | |
| `timeout_seconds` | see above | `60.0` | |
| `fuzz` | `float` (0–100) | `8.0` | Whiteness tolerance |

Output: `step_dir / {input_stem}.png`

### `image.trim`

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `backend` | see above | `"auto"` | |
| `timeout_seconds` | see above | `60.0` | |
| `border` | `int` (≥ 0) | `0` | Transparent padding re-added after trim |

Output: `step_dir / {input_stem}.png`

Before either backend runs, a shared bbox pre-check decides whether there is content to trim: when the
source image has an alpha channel, the alpha-channel bbox is used (falling back to RGBA `getbbox()` when
that bbox is empty); when there is no alpha channel, `getbbox()` on the source image applies — including
for the ImageMagick backend. If no bbox is found, both backends return `fail` with
`"nothing to trim: image is fully transparent"`.

Fully transparent images → `fail`, message `"nothing to trim: image is fully transparent"`.

### `image.export`

`OUTPUT_DIR_PARAMS = {"dest"}` — the runner may treat `dest` as an external output directory.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `dest` | `str` | *(required)* | Destination directory (`expanduser` at run) |
| `format` | `"png"` \| `"webp"` | `"png"` | Output format |
| `filename` | `str \| null` | `null` | Explicit basename override (highest priority) |
| `use_reserved_name` | `bool` | `true` | Use naming service when ordinal is set |
| `on_collision` | `"suffix"` \| `"replace"` \| `"fail"` | `"suffix"` | Existing file policy |

#### Export naming priority

1. `filename` param (if set)
2. `ctx.naming.resolve(ctx.ordinal)` when `use_reserved_name` and ordinal + naming are provided
3. Input filename stem

Extension handling: if the chosen name already ends with `.{format}` (e.g. reserved name `goat.png` with `format: png`), it is kept verbatim. Otherwise the extension is appended or replaced; a warning is logged when a different extension was replaced.

Example (game assets): ordinal `3` resolves to `goat.png` → file lands at `dest/goat.png` unchanged.

#### Collision policy

| `on_collision` | Behavior |
|----------------|----------|
| `suffix` | `name-2.png`, `name-3.png`, … |
| `replace` | Overwrite via atomic replace |
| `fail` | `fail` with `"destination exists: {path}"` |

#### Write protocol

1. `mkdir -p dest`
2. Write to `dest/.tmp-{uuid}`
3. `os.replace(tmp, final)`

When output format matches input, bytes are copied verbatim (no re-encode). Conversion uses Pillow when formats differ. Leftover `.tmp-*` files are removed on failure.

## Future work (out of scope)

- Formats beyond PNG/WebP
- ICC/color-profile handling
- EXIF preservation policies
- GIMP Script-Fu engine
- Pillow `getdata()` → `get_flattened_data()` migration (currently used in the white-to-alpha Pillow backend)

## Manual smoke test

```python
from pathlib import Path
from conveyor.core.steps import StepContext
from conveyor.core.workdir import TaskWorkdir
from conveyor.executors.headless.steps import WhiteToAlphaStep, TrimStep, ExportStep

workdir = TaskWorkdir.create(Path("/tmp/conveyor-demo"), "demo", 1)
src = Path("my-ai-image.png")
step_dir = workdir.step_dir(1, "image.white_to_alpha")
ctx = StepContext(
    task_id=1,
    pipeline_name="demo",
    source_ref=str(src),
    ordinal=None,
    input_path=src,
    step_dir=step_dir,
    logger=workdir.step_logger(step_dir),
)
out = WhiteToAlphaStep().run(ctx, WhiteToAlphaStep.Params()).output_path
# chain trim → export similarly
```

Verify transparency and crop in an image viewer; confirm the original file is unchanged.
