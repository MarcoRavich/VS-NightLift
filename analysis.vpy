"""Fast low-light probe for NightLift enhancer parameter tuning — StaxRip script step.

This is not a standalone CLI tool: it's meant to be dropped in as a StaxRip
script/tool step, where StaxRip textually substitutes %source_file%,
%temp_file% and %source_temp_file% before running the file with its bundled
Python. The source-loading block below is StaxRip's own template (left
untouched) — everything after it operates on the `clip` it produces.

Only one VapourSynth plugin is required: bs (BestSource), already used by
the loader block.

Design notes (see chat for full rationale):
- Only mean luma and dark-pixel ratio are computed: the only two metrics
  the pipeline decision actually uses. Median/bright-ratio/contrast/
  dynamic-range are gone, and with them the vszip and akarin plugin
  dependencies.
- Sampling is a simple evenly-spaced sweep across the 15-85% window
  (skips likely intro/outro), sized 5-12 frames by duration instead of
  up to 30. Frames are requested in parallel via get_frame_async.
- Waits for a keypress before exiting, so the printed report stays on
  screen when StaxRip's console window would otherwise close immediately.
- fpsnum=-1/rff=False in the loader means timing isn't forced to CFR;
  time_seconds is best-effort and left out if the clip has no usable fps.
"""

from __future__ import annotations

import json
import os
import statistics
from pathlib import Path
from typing import Any, Optional

import vapoursynth as vs

core = vs.core

# ---- StaxRip source loader (verbatim StaxRip template — do not edit) ----
SOURCE_PATH = r"%source_file%"
tcFile = r"%temp_file%_timestamps.txt"  # timestamps file path
clip = core.bs.VideoSource(r"%source_file%", track=-1, fpsnum=-1, fpsden=1, rff=False, threads=0, seekpreroll=20, enable_drefs=False, use_absolute_path=False, cachemode=3, cachepath=r"%source_temp_file%", cachesize=1000, hwdevice="", extrahwframes=9, timecodes=tcFile) if os.path.exists(tcFile) else core.bs.VideoSource(r"%source_file%", track=-1, fpsnum=-1, fpsden=1, rff=False, threads=0, seekpreroll=20, enable_drefs=False, use_absolute_path=False, cachemode=3, cachepath=r"%source_temp_file%", cachesize=1000, hwdevice="", extrahwframes=9)

# ---- analysis configuration ----
ANALYSIS_WIDTH = 480
SOURCE_RANGE = "limited"
SAMPLE_WINDOW = (15.0, 85.0)          # percent range to sweep, avoids intro/outro
DARK_THRESHOLD_16 = round(50 / 255 * 65535)
REPORT_PATH = r"%temp_file%_analysis.json"


def dark_level(dark_ratio: float, mean_luma: float) -> str:
    """Same decision ladder used by enhancer.vpy."""
    if dark_ratio > 0.7 and mean_luma < 40.0:
        return "extreme"
    if dark_ratio > 0.5 or mean_luma < 60.0:
        return "severe"
    if dark_ratio > 0.3 or mean_luma < 90.0:
        return "moderate"
    return "mild"


def _to_luma16(clip: vs.VideoNode, source_range: str) -> vs.VideoNode:
    """Extract the luma plane as full-range GRAY16 (matrix only matters for RGB)."""
    fmt = clip.format
    if fmt.color_family == vs.YUV:
        clip = core.std.ShufflePlanes(clip, planes=0, colorfamily=vs.GRAY)
        return core.resize.Point(clip, format=vs.GRAY16, range_in_s=source_range, range_s="full")
    if fmt.color_family == vs.RGB:
        return core.resize.Point(clip, format=vs.GRAY16, matrix_s="709", range_s="full")
    return core.resize.Point(clip, format=vs.GRAY16, range_in_s=source_range, range_s="full")


def build_analysis_clip(clip: vs.VideoNode, width: int, source_range: str) -> vs.VideoNode:
    """Lazy graph producing MeanLuma/DarkRatio frame props; nothing decodes until requested."""
    luma = _to_luma16(clip, source_range)
    if width and luma.width > width:
        height = max(1, round(luma.height * width / luma.width))
        luma = core.resize.Bilinear(luma, width=width, height=height)

    dark_mask = core.std.Expr(luma, f"x {DARK_THRESHOLD_16} < 65535 0 ?")
    mean_props = core.std.PlaneStats(luma, prop="M")
    dark_props = core.std.PlaneStats(dark_mask, prop="D")

    def _merge(n: int, f: list[vs.VideoFrame]) -> vs.VideoFrame:
        out = f[0].copy()
        out.props["MeanLuma"] = float(f[0].props["MAverage"]) * 255.0
        out.props["DarkRatio"] = float(f[1].props["DAverage"])
        return out

    return core.std.ModifyFrame(mean_props, [mean_props, dark_props], selector=_merge)


def suggest_pipeline(level: str, dark_ratio: float, mean_luma: float, varies: bool) -> dict:
    """Translate the aggregate metrics into enhancer.vpy's auto parameter policy."""
    pipeline: list[dict] = [{"operation": "gray_world"}]
    if level == "extreme":
        pipeline += [
            {"operation": "msrcp", "scales": [8.0, 50.0, 150.0]},
            {"operation": "clahe", "clip_limit": 3.0, "tile_grid": [8, 8]},
            {"operation": "gamma", "gamma": 0.35},
        ]
    elif level == "severe":
        pipeline += [
            {"operation": "clahe", "clip_limit": 2.5, "tile_grid": [8, 8]},
            {"operation": "gamma", "gamma": 0.45},
            {"operation": "unsharp", "sigma": 2.5, "amount": 0.4},
        ]
    elif level == "moderate":
        pipeline += [
            {"operation": "clahe", "clip_limit": 1.5 + dark_ratio, "tile_grid": [8, 8]},
            {"operation": "gamma", "gamma": 0.55 + dark_ratio * 0.2},
            {"operation": "unsharp", "sigma": 2.0, "amount": 0.3},
        ]
    else:
        pipeline.append({"operation": "clahe", "clip_limit": 1.2, "tile_grid": [8, 8]})
        if mean_luma < 100.0:
            pipeline.append({"operation": "gamma", "gamma": 0.75})

    return {
        "level": level,
        "application": "enhance_auto_per_frame" if varies else "fixed_pipeline",
        "operations": pipeline,
    }


def _wait_for_keypress() -> None:
    """Keep the console open so the printed values can be read before it closes."""
    print("\nPress any key to continue . . .", end="", flush=True)
    try:
        import msvcrt
        msvcrt.getch()
    except ImportError:
        input()  # non-Windows fallback: Enter key
    print()


def analyze(clip: vs.VideoNode) -> dict[str, Any]:
    fps = clip.fps
    has_fps = fps.numerator > 0 and fps.denominator > 0
    duration = clip.num_frames * fps.denominator / fps.numerator if has_fps else None

    n = max(5, min(12, round(duration / 400) + 5)) if duration is not None else 8
    lo, hi = SAMPLE_WINDOW
    positions = [(lo + hi) / 2] if n == 1 else [lo + i * (hi - lo) / (n - 1) for i in range(n)]
    last = clip.num_frames - 1
    indices = sorted({max(0, min(last, round(last * p / 100))) for p in positions})

    graph = build_analysis_clip(clip, ANALYSIS_WIDTH, SOURCE_RANGE)
    pending = [(i, graph.get_frame_async(i)) for i in indices]

    samples_out = []
    for i, future in pending:
        frame = future.result()
        mean_luma = float(frame.props["MeanLuma"])
        dark_ratio = float(frame.props["DarkRatio"])
        frame.close()
        samples_out.append({
            "frame": i,
            "time_seconds": round(i * fps.denominator / fps.numerator, 1) if has_fps else None,
            "mean_luma": round(mean_luma, 2),
            "dark_ratio": round(dark_ratio, 4),
            "level": dark_level(dark_ratio, mean_luma),
        })

    mean_med = statistics.median(s["mean_luma"] for s in samples_out)
    dark_med = statistics.median(s["dark_ratio"] for s in samples_out)
    level = dark_level(dark_med, mean_med)
    varies = len({s["level"] for s in samples_out}) > 1

    return {
        "source": {
            "path": SOURCE_PATH,
            "fps": f"{fps.numerator}/{fps.denominator}" if has_fps else None,
            "width": clip.width,
            "height": clip.height,
            "duration_seconds": round(duration, 1) if duration is not None else None,
        },
        "samples": samples_out,
        "median_mean_luma": round(mean_med, 2),
        "median_dark_ratio": round(dark_med, 4),
        "pipeline": suggest_pipeline(level, dark_med, mean_med, varies),
    }


# ---- run ----
try:
    report = analyze(clip)
except Exception as exc:
    print(f"analysis failed: {exc}")
    _wait_for_keypress()
    raise

payload = json.dumps(report, indent=2, ensure_ascii=False)
print(payload)
Path(REPORT_PATH).write_text(payload + "\n", encoding="utf-8")
_wait_for_keypress()
