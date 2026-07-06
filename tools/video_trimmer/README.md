# video_trimmer.py

Trim a long video into bite-sized clips using **libopenshot** (the C++ video
engine behind OpenShot Video Editor), driven by a JSON list of timestamps
(e.g. produced by a Gemini video-analysis pass).

## What's verified vs. not (read this first)

`openshot` (libopenshot's Python bindings) is **not published on PyPI**.
It was installed here via `sudo apt install python3-openshot` (Ubuntu 24.04,
package version `0.3.2+dfsg1-2.1build3`, pulled in `libopenshot25t64` and
`libopenshot-audio9t64`), and the whole tool was then run end-to-end against
the real module and a real video file (`src/resources/hardware-example.mp4`
from this repo) - not just written from memory. Three real bugs surfaced
during that testing and are already fixed in this version of the script:

1. **`PrepareStreams()` must be called once, not twice.** The reference
   pattern in `src/windows/export_clips.py` (`SetVideoOptions()` →
   `PrepareStreams()` → `SetAudioOptions()` → `PrepareStreams()` again) creates
   a duplicate video stream in libopenshot 0.3.2 and corrupts the output
   (wrong resolution on readback, missing frames). Fixed by calling
   `SetVideoOptions()` + `SetAudioOptions()` and only then `PrepareStreams()`
   once.
2. **`libx264` silently drops the video stream** in this specific
   apt-packaged build (every `WriteFrame()` call logged `Frame AVERROR_EOF`
   and the output ended up with no video track at all, or the wrong
   resolution). `libx265` and `mpeg4` both worked correctly and were verified
   to produce a valid video track at the requested resolution and frame
   count. This is almost certainly an ABI/version mismatch between the
   distro's `libopenshot25t64` and its `libx264`/`libavcodec` build, not a
   bug in this script - but since it's very possible you'll hit the same
   thing on a similarly-packaged system, a `--codec` flag is provided
   (default `libx264`, matching what real OpenShot installs expect). **If
   your exports come out empty, black, or the wrong resolution, re-run with
   `--codec libx265` first** to check whether you're hitting the same
   packaging issue.
3. **`Frame.Thumbnail()` takes 10 args, not 11**, in libopenshot 0.3.2 - it
   has no trailing `scale_mode` parameter, unlike the signature used in this
   repo's own `src/classes/thumbnail.py` (which targets a newer libopenshot).
   Fixed to match what's actually installed; a comment in the code explains
   how to add the 11th arg back on a newer libopenshot.

After those three fixes, a full run with `--vertical --thumbnails --codec
libx265` was verified to produce:
- a horizontal clip at the source resolution (1280x720),
- a vertical clip at exactly 1080x1920 (`Timeline` + `Clip.gravity =
  GRAVITY_CENTER` + `Clip.scale = SCALE_CROP` compositing),
- a non-black JPEG thumbnail containing real decoded frame pixels,
- and correct skip/warning behavior for an out-of-bounds cut entry.

Everything else in this tool (timestamp parsing, cuts.json validation, frame
math, dry-run bounds checking, error handling for malformed entries) is pure
Python and was unit-tested directly, independent of libopenshot.

**What's still unverified**: the sample video used for testing is a 0.3s,
10-frame test asset, so long-form, many-clip batches and audio-heavy content
haven't been exercised. Run `--dry-run` on your real video first, then
render one clip before batch-exporting everything.

## Installing libopenshot

There is no `pip install openshot`. Pick one:

1. **Distro package** (easiest if available, and what was used to verify this
   tool): on Debian/Ubuntu, `sudo apt install python3-openshot`. This pulls in
   `libopenshot` + `libopenshot-audio` automatically. Package availability
   varies by release - check `apt-cache search openshot`.
2. **OpenShot Video Editor install**: installing the `openshot-qt` app
   (AppImage, Windows/Mac installer, or `apt install openshot`) typically
   bundles a working `openshot` Python module you can point `PYTHONPATH` at.
3. **Build libopenshot from source** with Python bindings enabled (requires
   CMake, SWIG, FFmpeg dev libs, ImageMagick, etc.) - see
   https://github.com/OpenShot/libopenshot and
   https://github.com/OpenShot/libopenshot-audio for build instructions.

Whichever route you use, confirm `python3 -c "import openshot; print(openshot.OPENSHOT_VERSION_FULL)"`
works with the *same* Python interpreter you'll run `video_trimmer.py` with.
On Debian/Ubuntu specifically, the apt package installs for the system's
default `python3` (check with `readlink -f /etc/alternatives/python3`, or
just try `python3.12 -c "import openshot"`, `python3.11 -c "..."`, etc. if the
default `python3` doesn't have it) - it will **not** show up inside a venv
unless you point the venv at system site-packages.

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

# If exports come out empty/black/wrong-resolution, try a different codec
python video_trimmer.py --input input.mp4 --cuts cuts.json --output ./output --codec libx265
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

1. A real source video file (`input.mp4`) - the testing done here used a
   0.3-second placeholder asset, not a real tutorial-length video.
2. A real `cuts.json` (paste in your Gemini timestamp output, matching the
   schema above) - `cuts.example.json` is a placeholder only.
3. A working `openshot` Python install (see above). If you're on a
   similarly-packaged Linux distro, run `--dry-run` first, then render one
   clip, and if it comes out empty/black/wrong-resolution try `--codec
   libx265` (see the codec caveat above) before batch-exporting everything.
