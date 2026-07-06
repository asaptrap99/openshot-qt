#!/usr/bin/env python3
"""
video_trimmer.py - Trim a long video into bite-sized clips with libopenshot.

Reads a JSON list of {clip_number, start, end, title} cut instructions
(e.g. output from a Gemini video-analysis pass) and renders each one as a
standalone .mp4 using the `openshot` (libopenshot) Python bindings.

Verified against: libopenshot Python bindings as actually used by the
OpenShot Video Editor GUI (openshot-qt), specifically:
  - src/windows/export_clips.py  (Clip + FFmpegWriter frame-export loop)
  - src/classes/proxy_service.py (Clip.Reader().Json() metadata, LAYOUT_* )
  - src/classes/thumbnail.py     (Frame.Thumbnail(...) signature)
See README.md for exactly what was confirmed this way vs. adapted from
the documented libopenshot Timeline/Clip compositing API (used for
--vertical) that could not be executed in the development sandbox.
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass

try:
    import openshot
except ImportError:
    sys.stderr.write(
        "ERROR: could not import the 'openshot' module (libopenshot Python bindings).\n"
        "This package is NOT available on PyPI - see README.md for install options\n"
        "(build libopenshot from source, or install your distro's package, e.g.\n"
        "'python3-openshot' on Debian/Ubuntu, or the OpenShot AppImage's bundled build).\n"
    )
    sys.exit(1)

MAX_SHORT_SECONDS = 60.0
VERTICAL_WIDTH = 1080
VERTICAL_HEIGHT = 1920
THUMBNAIL_OFFSET_SECONDS = 1.0


# --------------------------------------------------------------------------
# Timestamp / cut-list parsing
# --------------------------------------------------------------------------

def parse_timestamp(value) -> float:
    """Parse 'HH:MM:SS', 'MM:SS', 'SS', or a plain number of seconds."""
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"timestamp cannot be negative: {value}")
        return float(value)

    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)

    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"unrecognized timestamp format: {value!r}")
    try:
        numeric_parts = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"unrecognized timestamp format: {value!r}")

    seconds = 0.0
    for part in numeric_parts:
        seconds = seconds * 60 + part
    return seconds


def slugify(name: str) -> str:
    """Make a filesystem-safe slug out of a clip title."""
    text = re.sub(r"[^\w\s-]", "", str(name).strip().lower())
    text = re.sub(r"[\s-]+", "_", text).strip("_")
    return text or "clip"


@dataclass
class CutSpec:
    clip_number: int
    title: str
    start: float
    end: float
    raw_start: str
    raw_end: str

    @property
    def duration(self) -> float:
        return self.end - self.start

    def base_name(self) -> str:
        return f"{self.clip_number:02d}_{slugify(self.title)}"


def load_cuts(cuts_path: str):
    """Load and validate cuts.json. Returns (valid_cuts, error_messages)."""
    if not os.path.isfile(cuts_path):
        raise FileNotFoundError(f"Cuts file not found: {cuts_path}")

    with open(cuts_path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as ex:
            raise ValueError(f"cuts.json is not valid JSON: {ex}")

    if not isinstance(data, list):
        raise ValueError("cuts.json must contain a JSON array of clip objects")

    cuts, errors = [], []
    for index, entry in enumerate(data):
        label = f"entry #{index}"
        try:
            if not isinstance(entry, dict):
                raise ValueError("expected a JSON object")
            clip_number = int(entry["clip_number"])
            label = f"clip_number {clip_number}"
            title = str(entry["title"]).strip()
            if not title:
                raise ValueError("title cannot be empty")
            start = parse_timestamp(entry["start"])
            end = parse_timestamp(entry["end"])
            if end <= start:
                raise ValueError(
                    f"end ({entry['end']}) must be after start ({entry['start']})"
                )
            cuts.append(
                CutSpec(
                    clip_number=clip_number,
                    title=title,
                    start=start,
                    end=end,
                    raw_start=str(entry["start"]),
                    raw_end=str(entry["end"]),
                )
            )
        except (KeyError, ValueError, TypeError) as ex:
            missing = ""
            if isinstance(ex, KeyError):
                missing = f"missing required field {ex}"
            errors.append(f"{label}: {missing or ex}")

    return cuts, errors


# --------------------------------------------------------------------------
# libopenshot helpers
# --------------------------------------------------------------------------

def get_video_info(path: str) -> dict:
    """Open a video with openshot.Clip and return its reader metadata as a dict."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Input video not found: {path}")

    clip = openshot.Clip(path)
    try:
        clip.Open()
    except Exception as ex:
        raise RuntimeError(
            f"Failed to open '{path}' with libopenshot - the file may be missing, "
            f"corrupt, or use a codec libopenshot/ffmpeg was not built to decode: {ex}"
        )
    try:
        info = json.loads(clip.Reader().Json())
    finally:
        clip.Close()
    return info


def video_duration_seconds(info: dict) -> float:
    fps = info["fps"]["num"] / info["fps"]["den"]
    duration = info.get("duration")
    if duration:
        return float(duration)
    return float(info.get("video_length", 0)) / fps


def start_and_end_frames(start_sec: float, end_sec: float, fps: float):
    """Same 1-indexed inclusive frame math openshot-qt uses for clip export."""
    start_frame = max(1, int(round(start_sec * fps)) + 1)
    end_frame = max(start_frame - 1, int(round(end_sec * fps)))
    return start_frame, end_frame


def channel_layout_for(channels: int):
    return openshot.LAYOUT_STEREO if channels > 1 else openshot.LAYOUT_MONO


def export_clip(input_path: str, cut: CutSpec, output_path: str, info: dict):
    """Render a single trimmed clip at source resolution using Clip + FFmpegWriter."""
    fps_frac = openshot.Fraction(info["fps"]["num"], info["fps"]["den"])
    fps_float = info["fps"]["num"] / info["fps"]["den"]
    width = int(info.get("width", 1280))
    height = int(info.get("height", 720))
    pr = info.get("pixel_ratio", {"num": 1, "den": 1})
    pixel_ratio = openshot.Fraction(pr.get("num", 1), pr.get("den", 1))
    has_audio = bool(info.get("has_audio", False))
    sample_rate = int(info.get("sample_rate") or 48000)
    channels = int(info.get("channels") or 2)
    layout = channel_layout_for(channels)

    clip_reader = openshot.Clip(input_path)
    clip_reader.Open()

    writer = openshot.FFmpegWriter(output_path)
    try:
        writer.SetVideoOptions(
            True, "libx264", fps_frac, width, height, pixel_ratio, False, False, 22
        )
        writer.PrepareStreams()
        writer.SetAudioOptions(has_audio, "aac", sample_rate, channels, layout, 192000)
        writer.PrepareStreams()
        writer.Open()

        start_frame, end_frame = start_and_end_frames(cut.start, cut.end, fps_float)
        for frame_number in range(start_frame, end_frame + 1):
            writer.WriteFrame(clip_reader.GetFrame(frame_number))
    finally:
        writer.Close()
        clip_reader.Close()


def export_vertical_clip(input_path: str, cut: CutSpec, output_path: str, info: dict):
    """
    Render a 9:16 (1080x1920) center-cropped version of the clip using an
    openshot.Timeline + Clip with gravity/scale set to GRAVITY_CENTER / SCALE_CROP.

    NOTE: this path uses the documented libopenshot Timeline/Clip compositing
    API (Clip.gravity, Clip.scale, Clip.Position/Start/End/Layer, Timeline.AddClip)
    which could not be executed in the development sandbox (libopenshot isn't
    installed there). See README.md "What's verified vs. not" before relying
    on this in production - test it against your real libopenshot build first.
    """
    fps_frac = openshot.Fraction(info["fps"]["num"], info["fps"]["den"])
    fps_float = info["fps"]["num"] / info["fps"]["den"]
    has_audio = bool(info.get("has_audio", False))
    sample_rate = int(info.get("sample_rate") or 48000)
    channels = int(info.get("channels") or 2)
    layout = channel_layout_for(channels)

    timeline = openshot.Timeline(
        VERTICAL_WIDTH, VERTICAL_HEIGHT, fps_frac, sample_rate, channels, layout
    )
    timeline.Open()

    clip = openshot.Clip(input_path)
    clip.gravity = openshot.GRAVITY_CENTER
    clip.scale = openshot.SCALE_CROP
    clip.Layer(1)
    clip.Position(0.0)
    clip.Start(cut.start)
    clip.End(cut.end)
    timeline.AddClip(clip)

    writer = openshot.FFmpegWriter(output_path)
    try:
        writer.SetVideoOptions(
            True, "libx264", fps_frac, VERTICAL_WIDTH, VERTICAL_HEIGHT,
            openshot.Fraction(1, 1), False, False, 22,
        )
        writer.PrepareStreams()
        writer.SetAudioOptions(has_audio, "aac", sample_rate, channels, layout, 192000)
        writer.PrepareStreams()
        writer.Open()

        start_frame, end_frame = start_and_end_frames(cut.start, cut.end, fps_float)
        length_frames = end_frame - start_frame + 1
        # Clip.Start()/End() already trim the source; timeline frame 1
        # corresponds to cut.start, so we read frames 1..length_frames.
        for i in range(1, length_frames + 1):
            writer.WriteFrame(timeline.GetFrame(i))
    finally:
        writer.Close()
        timeline.Close()


def generate_thumbnail(rendered_clip_path: str, thumb_path: str,
                        at_seconds: float = THUMBNAIL_OFFSET_SECONDS):
    """Grab a frame from a rendered clip and save it as a JPEG thumbnail."""
    reader = openshot.Clip(rendered_clip_path)
    reader.Open()
    try:
        info = json.loads(reader.Reader().Json())
        fps_float = info["fps"]["num"] / info["fps"]["den"]
        video_length = int(info.get("video_length", 1)) or 1
        frame_number = max(1, int(round(at_seconds * fps_float)) + 1)
        frame_number = min(frame_number, video_length)
        frame = reader.GetFrame(frame_number)
        frame.Thumbnail(
            thumb_path,
            int(info.get("width", 1280)),
            int(info.get("height", 720)),
            "", "", "#000", False, "jpeg", 90, 0.0,
            openshot.SCALE_CROP,
        )
    finally:
        reader.Close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def format_seconds(value: float) -> str:
    minutes, seconds = divmod(max(0.0, value), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{int(hours)}:{int(minutes):02d}:{seconds:05.2f}"
    return f"{int(minutes)}:{seconds:05.2f}"


def bounds_problems(cut: CutSpec, duration: float):
    problems = []
    if cut.start < 0:
        problems.append("start is negative")
    if cut.end > duration + 0.5:  # small tolerance for rounding
        problems.append(f"end ({format_seconds(cut.end)}) exceeds video length ({format_seconds(duration)})")
    return problems


def run_dry_run(cuts, info):
    duration = video_duration_seconds(info)
    fps_float = info["fps"]["num"] / info["fps"]["den"]
    print(f"Source video: {format_seconds(duration)} long, "
          f"{info.get('width')}x{info.get('height')} @ {fps_float:.3f}fps, "
          f"has_audio={info.get('has_audio')}")
    print(f"{'#':>3}  {'title':<32} {'start':>10} {'end':>10} {'duration':>10}  status")
    any_problem = False
    for cut in cuts:
        problems = bounds_problems(cut, duration)
        status = "OK" if not problems else "OUT OF BOUNDS: " + "; ".join(problems)
        if cut.duration > MAX_SHORT_SECONDS:
            status += (" [WARN: >60s]" if not problems else "")
        if problems:
            any_problem = True
        print(f"{cut.clip_number:>3}  {cut.title[:32]:<32} "
              f"{format_seconds(cut.start):>10} {format_seconds(cut.end):>10} "
              f"{format_seconds(cut.duration):>10}  {status}")
    return 1 if any_problem else 0


def main():
    parser = argparse.ArgumentParser(
        description="Trim a long video into bite-sized clips using libopenshot."
    )
    parser.add_argument("--input", required=True, help="Path to the source video file")
    parser.add_argument("--cuts", required=True, help="Path to cuts.json")
    parser.add_argument("--output", required=True, help="Output folder for rendered clips")
    parser.add_argument("--dry-run", action="store_true",
                         help="Resolve and validate timestamps without rendering")
    parser.add_argument("--vertical", action="store_true",
                         help="Also export a 9:16 (1080x1920) cropped version of each clip")
    parser.add_argument("--thumbnails", action="store_true",
                         help="Save a JPEG thumbnail (1s in) for each rendered clip")
    parser.add_argument("--max-duration", type=float, default=MAX_SHORT_SECONDS,
                         help=f"Warn (don't fail) if a clip exceeds this many seconds "
                              f"(default: {MAX_SHORT_SECONDS})")
    args = parser.parse_args()

    # --- Load + validate cuts ---
    try:
        cuts, cut_errors = load_cuts(args.cuts)
    except (FileNotFoundError, ValueError) as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        return 1

    for err in cut_errors:
        print(f"WARNING: skipping malformed cut - {err}", file=sys.stderr)

    if not cuts:
        print("ERROR: no valid cut entries found in cuts.json", file=sys.stderr)
        return 1

    # --- Open source video / read metadata ---
    try:
        info = get_video_info(args.input)
    except (FileNotFoundError, RuntimeError) as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        return 1

    duration = video_duration_seconds(info)

    if args.dry_run:
        return run_dry_run(cuts, info)

    os.makedirs(args.output, exist_ok=True)

    results = []  # list of dicts describing each attempted clip
    total_render_start = time.time()

    for cut in cuts:
        problems = bounds_problems(cut, duration)
        if problems:
            print(f"SKIP clip {cut.clip_number} ({cut.title}): " + "; ".join(problems),
                  file=sys.stderr)
            results.append({"cut": cut, "status": "skipped", "reason": "; ".join(problems)})
            continue

        base_name = cut.base_name()
        output_path = os.path.join(args.output, f"{base_name}.mp4")
        print(f"Rendering clip {cut.clip_number} ({cut.title}) -> {output_path}")

        clip_start = time.time()
        try:
            export_clip(args.input, cut, output_path, info)
        except Exception as ex:
            print(f"ERROR exporting clip {cut.clip_number}: {ex}", file=sys.stderr)
            traceback.print_exc()
            if os.path.exists(output_path):
                os.remove(output_path)
            results.append({"cut": cut, "status": "failed", "reason": str(ex)})
            continue
        render_time = time.time() - clip_start

        out_info = None
        try:
            out_info = get_video_info(output_path)
        except Exception as ex:
            print(f"WARNING: could not re-open rendered clip to validate duration: {ex}",
                  file=sys.stderr)

        out_duration = video_duration_seconds(out_info) if out_info else cut.duration
        over_limit = out_duration > args.max_duration
        if over_limit:
            print(f"WARNING: clip {cut.clip_number} is {format_seconds(out_duration)}, "
                  f"over the {args.max_duration:.0f}s limit (continuing anyway)")

        file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0

        thumb_path = None
        if args.thumbnails:
            thumb_path = os.path.join(args.output, f"{slugify(cut.title)}.jpg")
            try:
                generate_thumbnail(output_path, thumb_path)
            except Exception as ex:
                print(f"WARNING: thumbnail generation failed for clip {cut.clip_number}: {ex}",
                      file=sys.stderr)
                thumb_path = None

        vertical_path = None
        if args.vertical:
            vertical_path = os.path.join(args.output, f"{base_name}_vertical.mp4")
            try:
                export_vertical_clip(args.input, cut, vertical_path, info)
            except Exception as ex:
                print(f"WARNING: vertical export failed for clip {cut.clip_number}: {ex}",
                      file=sys.stderr)
                if os.path.exists(vertical_path):
                    os.remove(vertical_path)
                vertical_path = None

        results.append({
            "cut": cut,
            "status": "ok",
            "output_path": output_path,
            "duration": out_duration,
            "over_limit": over_limit,
            "file_size": file_size,
            "render_time": render_time,
            "thumb_path": thumb_path,
            "vertical_path": vertical_path,
        })

    total_render_time = time.time() - total_render_start
    print_summary(results, total_render_time)

    return 0 if any(r["status"] == "ok" for r in results) else 1


def print_summary(results, total_render_time):
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]
    over_limit = [r for r in ok if r["over_limit"]]

    print("\n===== Summary =====")
    print(f"Clips created:   {len(ok)}")
    print(f"Clips failed:    {len(failed)}")
    print(f"Clips skipped:   {len(skipped)}")
    print(f"Total render time: {total_render_time:.1f}s")

    if ok:
        print("\nRendered files:")
        for r in ok:
            size_mb = r["file_size"] / (1024 * 1024)
            flag = " [OVER 60s]" if r["over_limit"] else ""
            print(f"  - {os.path.basename(r['output_path'])}: "
                  f"{format_seconds(r['duration'])}, {size_mb:.1f} MB{flag}")
            if r.get("vertical_path"):
                print(f"      + vertical: {os.path.basename(r['vertical_path'])}")
            if r.get("thumb_path"):
                print(f"      + thumbnail: {os.path.basename(r['thumb_path'])}")

    if over_limit:
        print(f"\nClips over the duration limit: "
              f"{', '.join(str(r['cut'].clip_number) for r in over_limit)}")

    if failed:
        print("\nFailed clips:")
        for r in failed:
            print(f"  - clip {r['cut'].clip_number} ({r['cut'].title}): {r['reason']}")

    if skipped:
        print("\nSkipped clips (out of bounds):")
        for r in skipped:
            print(f"  - clip {r['cut'].clip_number} ({r['cut'].title}): {r['reason']}")


if __name__ == "__main__":
    sys.exit(main())
