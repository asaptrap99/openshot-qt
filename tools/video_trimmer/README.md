# video_trimmer.py

Trim a long video into bite-sized clips using **libopenshot** (the C++ video
engine behind OpenShot Video Editor), driven by a JSON list of timestamps
(e.g. produced by a Gemini video-analysis pass).

## What's verified vs. not (read this first)

The `openshot` Python module is **not published on PyPI** and is not
installed in the environment this tool was built in, so it could not be
imported or executed there. To avoid guessing at API names from memory, the
frame-export code in this tool was copied/adapted directly from the actual,
working libopenshot usage inside the `openshot-qt` GUI source tree:

| Piece | Confirmed against |
|---|---|
| `openshot.Clip(path)`, `.Open()`, `.GetFrame(n)`, `.Close()` | `src/windows/export_clips.py` |
| `clip.Reader().Json()` metadata (fps, width, height, duration, has_audio, sample_rate, channels) | `src/classes/proxy_service.py` |
| `openshot.FFmpegWriter`: `SetVideoOptions`/`SetAudioOptions`/`PrepareStreams`/`Open`/`WriteFrame`/`Close` | `src/windows/export_clips.py` |
| `openshot.Fraction(num, den)` | `src/classes/timeline.py`, `proxy_service.py` |
| `frame.Thumbnail(path, w, h, mask, overlay, bg, ignore_aspect, format, quality, rotate, scale_mode)` | `src/classes/thumbnail.py` |
| `openshot.LAYOUT_STEREO` / `LAYOUT_MONO`, `openshot.SCALE_CROP`, `openshot.GRAVITY_TOP` (and by extension `GRAVITY_CENTER`) | `proxy_service.py`, `thumbnail.py`, `exporters/final_cut_pro.py` |

The **`--vertical` flag** (9:16 crop) uses `openshot.Timeline` +
`Clip.gravity` / `Clip.scale` / `Clip.Position()` / `.Start()` / `.End()` /
`.Layer()` / `Timeline.AddClip()`. These are documented libopenshot
`Clip`/`Timeline` C++ API members (SWIG-exposed 1:1 to Python), but this repo
only uses them indirectly through its JSON project-data model, not by
scripting a bare `Timeline` object like this tool does - so **this path
could not be verified by direct precedent in this codebase, and could not be
executed at all in the sandbox**. Test `--vertical` on a real libopenshot
install before relying on it. If clip compositing behaves differently than
expected (e.g. the crop doesn't fill the frame), the likely fix is adjusting
`clip.scale_x` / `clip.scale_y` / `clip.location_x` / `clip.location_y`
(these are `openshot.Keyframe` objects, e.g. `clip.location_x = openshot.Keyframe(0.0)`)
rather than the plain `gravity`/`scale` enums.

Everything else in this tool (timestamp parsing, cuts.json validation, frame
math, dry-run bounds checking, thumbnail frame calculation, summary
reporting) is pure Python and has been unit-tested directly.

## Installing libopenshot

There is no `pip install openshot`. Pick one:

1. **Distro package** (easiest if available): e.g. on Debian/Ubuntu,
   `sudo apt install python3-openshot` (package name/availability varies by
   release - check `apt-cache search openshot`).
2. **OpenShot Video Editor install**: installing the `openshot-qt` app
   (AppImage, Windows/Mac installer, or `apt install openshot`) typically
   bundles a working `openshot` Python module you can point `PYTHONPATH` at.
3. **Build libopenshot from source** with Python bindings enabled (requires
   CMake, SWIG, FFmpeg dev libs, ImageMagick, etc.) - see
   https://github.com/OpenShot/libopenshot and
   https://github.com/OpenShot/libopenshot-audio for build instructions.

Whichever route you use, confirm `python3 -c "import openshot; print(openshot.OPENSHOT_VERSION_FULL)"`
works with the *same* Python interpreter you'll run `video_trimmer.py` with.

## Input format

`cuts.json` - a JSON array of clip definitions:

```json
[
  {"clip_number": 1, "start": "00:01:15", "end": "00:02:10", "title": "how_to_install_dependencies"},
  {"clip_number": 2, "start": "00:03:40", "end": "00:04:35", "title": "writing_your_first_function"}
]
```

- `start`/`end` accept `HH:MM:SS`, `MM:SS`, or plain seconds (`"90"` or `90`).
- `title` is slugified into the output filename.
- Malformed entries (missing fields, bad timestamps, `end <= start`) are
  **skipped with a warning**, not fatal - other valid entries still render.
- Entries whose `end` exceeds the source video's duration are skipped with a
  warning at render time (and flagged in `--dry-run`).

See `cuts.example.json` for a working example.

## Usage

```bash
# Validate timestamps and bounds without rendering anything
python video_trimmer.py --input input.mp4 --cuts cuts.json --output ./output --dry-run

# Render all clips
python video_trimmer.py --input input.mp4 --cuts cuts.json --output ./output

# Also render a 9:16 cropped version of each clip (see caveat above)
python video_trimmer.py --input input.mp4 --cuts cuts.json --output ./output --vertical

# Also save a JPEG thumbnail (1s into each clip)
python video_trimmer.py --input input.mp4 --cuts cuts.json --output ./output --thumbnails

# Change the "warn if longer than" threshold (default 60s)
python video_trimmer.py --input input.mp4 --cuts cuts.json --output ./output --max-duration 90
```

Outputs land in `--output` as `{clip_number:02d}_{title}.mp4`, plus
`{clip_number:02d}_{title}_vertical.mp4` and `{title}.jpg` when the
respective flags are passed.

## Error handling

- **Missing input video / cuts.json**: fails fast with a clear message.
- **Malformed JSON / malformed individual cut entries**: whole file rejected
  if the JSON itself is invalid; individual bad entries are skipped with a
  warning while good entries still render.
- **`end` before `start`**: entry skipped with a warning.
- **Timestamp beyond video length**: entry skipped with a warning (checked
  against the real duration read from the source file via libopenshot).
- **Unsupported codec / corrupt file**: `openshot.Clip.Open()` failure is
  caught and reported with a clear message instead of crashing.
- **Clip over the duration limit** (default 60s): rendered anyway, flagged
  as a warning in the final summary - never fails the run.

## What you need to provide to run this end-to-end

1. A real source video file (`input.mp4`).
2. A real `cuts.json` (paste in your Gemini timestamp output, matching the
   schema above) - `cuts.example.json` is a placeholder only.
3. A working `openshot` Python install (see above) - this could not be
   verified/executed in the sandbox this tool was built in, so please run
   `--dry-run` first and sanity-check one real clip before batch-exporting
   everything.
