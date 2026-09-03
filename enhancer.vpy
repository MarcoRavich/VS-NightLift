"""Native VapourSynth port of enhancer.py.

No OpenCV or NumPy frame processing is used. Pixel work is performed by
VapourSynth filters/plugins. Per-frame Python callbacks only read frame
properties to select a branch or construct a small native Expr operation.

Required plugin namespaces by method:
    bs       - BestSource (only for the executable example at the bottom)
    vszip    - CLAHE and percentile PlaneMinMax
    grwrld   - Gray-World white balance
    retinex  - MSRCP and MSRCR/SSR
    dhce     - Dehazing (only for method="dehaze")

Typical VSRepo installation:
    vsrepo install bs vszip grwrld retinex dhce

Edit SOURCE, METHOD and COLOR at the bottom, or set VS_SOURCE and VS_METHOD.
"""

from __future__ import annotations

import functools
import math
import os
from dataclasses import dataclass
from typing import Callable, Dict, Sequence, Tuple

import vapoursynth as vs


core = vs.core


@dataclass(frozen=True)
class ColorConfig:
    """Explicit metadata used for YUV/RGB and linear-light conversions."""

    matrix: str = "709"
    transfer: str = "709"
    source_range: str = "limited"
    chroma_location: str = "left"


DEFAULT_COLOR = ColorConfig()


def _validate_clip(clip: vs.VideoNode) -> None:
    if not isinstance(clip, vs.VideoNode):
        raise TypeError("clip must be a VapourSynth VideoNode")
    if clip.format is None:
        raise ValueError("variable-format clips are not supported")
    if clip.format.color_family not in (vs.RGB, vs.YUV) or clip.format.num_planes != 3:
        raise ValueError("a constant-format, three-plane RGB or YUV clip is required")


def _plugin_function(namespace: str, function: str) -> Callable:
    try:
        return getattr(getattr(core, namespace), function)
    except AttributeError as exc:
        raise RuntimeError(
            f"required VapourSynth plugin is missing: core.{namespace}.{function}"
        ) from exc


def _to_rgb48(clip: vs.VideoNode, cfg: ColorConfig) -> vs.VideoNode:
    _validate_clip(clip)
    if clip.format.color_family == vs.RGB:
        return core.resize.Point(clip, format=vs.RGB48)
    return core.resize.Spline36(
        clip,
        format=vs.RGB48,
        matrix_in_s=cfg.matrix,
        range_in_s=cfg.source_range,
        chromaloc_in_s=cfg.chroma_location,
    )


def _from_rgb(clip: vs.VideoNode, reference: vs.VideoNode, cfg: ColorConfig) -> vs.VideoNode:
    if reference.format is None:
        raise ValueError("reference clip must have a constant format")
    if reference.format.color_family == vs.RGB:
        return core.resize.Point(clip, format=reference.format.id)
    return core.resize.Spline36(
        clip,
        format=reference.format.id,
        matrix_s=cfg.matrix,
        range_s=cfg.source_range,
        chromaloc_s=cfg.chroma_location,
    )


def _to_rgbs_encoded(clip: vs.VideoNode, cfg: ColorConfig) -> vs.VideoNode:
    return core.resize.Point(_to_rgb48(clip, cfg), format=vs.RGBS)


def _to_yuv444p16(clip: vs.VideoNode, cfg: ColorConfig) -> vs.VideoNode:
    _validate_clip(clip)
    if clip.format.color_family == vs.YUV:
        return core.resize.Spline36(
            clip,
            format=vs.YUV444P16,
            range_in_s=cfg.source_range,
            range_s="full",
            chromaloc_in_s=cfg.chroma_location,
        )
    return core.resize.Spline36(
        clip,
        format=vs.YUV444P16,
        matrix_s=cfg.matrix,
        range_s="full",
    )


def _from_yuv444p16(
    clip: vs.VideoNode, reference: vs.VideoNode, cfg: ColorConfig
) -> vs.VideoNode:
    if reference.format is None:
        raise ValueError("reference clip must have a constant format")
    if reference.format.color_family == vs.YUV:
        return core.resize.Spline36(
            clip,
            format=reference.format.id,
            range_in_s="full",
            range_s=cfg.source_range,
            chromaloc_s=cfg.chroma_location,
        )
    return core.resize.Spline36(
        clip,
        format=reference.format.id,
        matrix_in_s=cfg.matrix,
        range_in_s="full",
    )


def _luma16(clip: vs.VideoNode, cfg: ColorConfig) -> vs.VideoNode:
    yuv = _to_yuv444p16(clip, cfg)
    return core.std.ShufflePlanes(yuv, planes=0, colorfamily=vs.GRAY)


def enhance_clahe(
    clip: vs.VideoNode,
    clip_limit: float = 2.0,
    tile_grid: Tuple[int, int] = (8, 8),
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Apply vszip CLAHE to full-range 16-bit luma and preserve chroma."""
    if clip_limit <= 0:
        raise ValueError("clip_limit must be positive")
    if len(tile_grid) != 2 or min(tile_grid) < 1:
        raise ValueError("tile_grid must contain two positive integers")

    yuv = _to_yuv444p16(clip, cfg)
    luma = core.std.ShufflePlanes(yuv, planes=0, colorfamily=vs.GRAY)
    clahe = _plugin_function("vszip", "CLAHE")

    # vszip documents a 256x limit scale for 16-bit input.
    limit16 = max(1, int(round(clip_limit * 256.0)))
    luma_eq = clahe(luma, limit=limit16, tiles=list(tile_grid))
    merged = core.std.ShufflePlanes(
        clips=[luma_eq, yuv],
        planes=[0, 1, 2],
        colorfamily=vs.YUV,
        prop_src=yuv,
    )
    return _from_yuv444p16(merged, clip, cfg)


def enhance_gamma(
    clip: vs.VideoNode,
    gamma: float = 0.5,
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Apply the OpenCV-style power curve independently to encoded RGB."""
    if gamma <= 0:
        raise ValueError("gamma must be positive")
    rgb = _to_rgbs_encoded(clip, cfg)
    # Expr documents pow() as requiring a strictly positive finite result.
    # 1e-12 still quantizes back to legal black at normal integer bit depths.
    expr = f"x 0 max 1 min 1e-12 max {gamma:.12g} pow"
    corrected = core.std.Expr(rgb, expr=[expr])
    return _from_rgb(corrected, clip, cfg)


def _levels_eval(
    n: int,
    frames: Sequence[vs.VideoFrame],
    rgb: vs.VideoNode,
    peak: int,
) -> vs.VideoNode:
    del n
    exprs = []
    for frame, prop in zip(frames, ("ALR", "ALG", "ALB")):
        low = int(frame.props[prop + "Min"])
        high = int(frame.props[prop + "Max"])
        if high <= low:
            high = low + 1
        exprs.append(f"x {low} - {peak} * {high - low} / 0 max {peak} min")
    return core.std.Expr(rgb, expr=exprs)


def enhance_auto_levels(
    clip: vs.VideoNode,
    low_percent: float = 0.5,
    high_percent: float = 99.5,
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Stretch each RGB channel between per-frame percentile endpoints."""
    if not 0.0 <= low_percent < high_percent <= 100.0:
        raise ValueError("percentiles must satisfy 0 <= low < high <= 100")

    rgb = _to_rgb48(clip, cfg)
    plane_minmax = _plugin_function("vszip", "PlaneMinMax")
    minthr = low_percent / 100.0
    maxthr = (100.0 - high_percent) / 100.0
    props = [
        plane_minmax(
            rgb,
            minthr=minthr,
            maxthr=maxthr,
            planes=[plane],
            prop=prop,
        )
        for plane, prop in enumerate(("ALR", "ALG", "ALB"))
    ]
    stretched = core.std.FrameEval(
        rgb,
        functools.partial(_levels_eval, rgb=rgb, peak=65535),
        prop_src=props,
        clip_src=[rgb],
    )
    return _from_rgb(stretched, clip, cfg)


def enhance_msrcp(
    clip: vs.VideoNode,
    scales: Sequence[float] = (15.0, 80.0, 250.0),
    lower_thr: float = 0.0,
    upper_thr: float = 0.0,
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Use the native Retinex MSRCP implementation on full-range RGB48."""
    if not scales or min(scales) <= 0:
        raise ValueError("scales must contain positive sigma values")
    if min(lower_thr, upper_thr) < 0 or lower_thr + upper_thr >= 1:
        raise ValueError("invalid Retinex normalization thresholds")

    rgb = _to_rgb48(clip, cfg)
    msrcp = _plugin_function("retinex", "MSRCP")
    result = msrcp(
        rgb,
        sigma=[float(value) for value in scales],
        lower_thr=lower_thr,
        upper_thr=upper_thr,
        fulls=True,
        fulld=True,
    )
    return _from_rgb(result, clip, cfg)


def auto_white_balance(
    clip: vs.VideoNode,
    robust_median: bool = False,
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Apply the grwrld plugin in the linear RGBS domain it requires."""
    rgb = _to_rgb48(clip, cfg)
    linear = core.resize.Spline36(
        rgb,
        format=vs.RGBS,
        transfer_in_s=cfg.transfer,
        transfer_s="linear",
    )
    grayworld = _plugin_function("grwrld", "grayworld")
    balanced = grayworld(linear, opt=-1, cc=1 if robust_median else 0)
    encoded = core.resize.Spline36(
        balanced,
        format=vs.RGB48,
        transfer_in_s="linear",
        transfer_s=cfg.transfer,
    )
    return _from_rgb(encoded, clip, cfg)


def _perfect_reflector_eval(
    n: int,
    frames: Sequence[vs.VideoFrame],
    rgb: vs.VideoNode,
    peak: int,
) -> vs.VideoNode:
    del n
    exprs = []
    for frame, prop in zip(frames, ("PRR", "PRG", "PRB")):
        white = int(frame.props[prop + "Max"])
        gain = 1.0 if white <= 0 else peak / white
        exprs.append(f"x {gain:.12g} * 0 max {peak} min")
    return core.std.Expr(rgb, expr=exprs)


def auto_white_balance_perfect_reflector(
    clip: vs.VideoNode,
    white_percentile: float = 99.0,
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Scale RGB channels so their selected high percentile reaches white."""
    if not 0.0 < white_percentile <= 100.0:
        raise ValueError("white_percentile must be in (0, 100]")

    rgb = _to_rgb48(clip, cfg)
    plane_minmax = _plugin_function("vszip", "PlaneMinMax")
    maxthr = (100.0 - white_percentile) / 100.0
    props = [
        plane_minmax(rgb, maxthr=maxthr, planes=[plane], prop=prop)
        for plane, prop in enumerate(("PRR", "PRG", "PRB"))
    ]
    balanced = core.std.FrameEval(
        rgb,
        functools.partial(_perfect_reflector_eval, rgb=rgb, peak=65535),
        prop_src=props,
        clip_src=[rgb],
    )
    return _from_rgb(balanced, clip, cfg)


def enhance_brightness_contrast(
    clip: vs.VideoNode,
    brightness: int = 30,
    contrast: float = 1.3,
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Apply result = RGB * contrast + brightness in the encoded RGB domain."""
    if not -255 <= brightness <= 255:
        raise ValueError("brightness must be between -255 and 255")
    if contrast < 0:
        raise ValueError("contrast must be non-negative")
    rgb = _to_rgbs_encoded(clip, cfg)
    offset = brightness / 255.0
    expr = f"x {contrast:.12g} * {offset:.12g} + 0 max 1 min"
    result = core.std.Expr(rgb, expr=[expr])
    return _from_rgb(result, clip, cfg)


def enhance_ssr(
    clip: vs.VideoNode,
    sigma: float = 80.0,
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Single-scale, per-RGB-channel Retinex via MSRCR with restore=0."""
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    rgb = _to_rgb48(clip, cfg)
    msrcr = _plugin_function("retinex", "MSRCR")
    result = msrcr(
        rgb,
        sigma=[float(sigma)],
        lower_thr=0.0,
        upper_thr=0.0,
        fulls=True,
        fulld=True,
        restore=0.0,
    )
    return _from_rgb(result, clip, cfg)


def enhance_dehaze(
    clip: vs.VideoNode,
    trans: float = 0.3,
    gamma: float = 1.5,
    air_size: int = 200,
    trans_size: int = 16,
    guide_size: int = 40,
    post: bool = False,
    lamda: float = 5.0,
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Use DehazingCE; this replaces, but is not identical to, OpenCV DCP."""
    rgb = _to_rgb48(clip, cfg)
    ref_width = min(320, rgb.width)
    ref_height = min(240, rgb.height)
    ref = core.resize.Spline36(rgb, width=ref_width, height=ref_height)
    dehazing = _plugin_function("dhce", "Dehazing")
    result = dehazing(
        rgb,
        ref=ref,
        trans=trans,
        gamma=gamma,
        air_size=air_size,
        trans_size=trans_size,
        guide_size=guide_size,
        post=int(post),
        lamda=lamda,
    )
    return _from_rgb(result, clip, cfg)


def _gaussian_blur(clip: vs.VideoNode, sigma: float) -> vs.VideoNode:
    if sigma <= 0:
        return clip
    radius = min(12, max(1, int(math.ceil(3.0 * sigma))))
    kernel = [
        math.exp(-(offset * offset) / (2.0 * sigma * sigma))
        for offset in range(-radius, radius + 1)
    ]
    divisor = sum(kernel)
    horizontal = core.std.Convolution(
        clip, matrix=kernel, divisor=divisor, mode="h"
    )
    return core.std.Convolution(
        horizontal, matrix=kernel, divisor=divisor, mode="v"
    )


def _unsharp(
    clip: vs.VideoNode,
    sigma: float,
    amount: float,
    cfg: ColorConfig,
) -> vs.VideoNode:
    rgb = _to_rgbs_encoded(clip, cfg)
    blurred = _gaussian_blur(rgb, sigma)
    expr = f"x {1.0 + amount:.12g} * y {amount:.12g} * - 0 max 1 min"
    sharpened = core.std.Expr([rgb, blurred], expr=[expr])
    return _from_rgb(sharpened, clip, cfg)


def enhance_comprehensive(
    clip: vs.VideoNode, cfg: ColorConfig = DEFAULT_COLOR
) -> vs.VideoNode:
    result = auto_white_balance(clip, cfg=cfg)
    result = enhance_clahe(result, clip_limit=1.5, tile_grid=(8, 8), cfg=cfg)
    result = enhance_gamma(result, gamma=0.7, cfg=cfg)
    return _unsharp(result, sigma=2.0, amount=0.5, cfg=cfg)


def enhance_strong(
    clip: vs.VideoNode, cfg: ColorConfig = DEFAULT_COLOR
) -> vs.VideoNode:
    result = enhance_msrcp(
        clip,
        scales=(10.0, 60.0, 180.0),
        lower_thr=0.0,
        upper_thr=0.0,
        cfg=cfg,
    )
    result = enhance_clahe(result, clip_limit=2.5, tile_grid=(8, 8), cfg=cfg)
    return enhance_gamma(result, gamma=0.6, cfg=cfg)


def _attach_analysis_props(
    n: int, frames: Sequence[vs.VideoFrame]
) -> vs.VideoFrame:
    del n
    output = frames[0].copy()
    mean_lum = float(frames[1].props["AnaMeanAverage"]) * 255.0
    median_lum = int(frames[2].props["AnaMedianMin"]) * 255.0 / 65535.0
    low = int(frames[3].props["AnaRangeMin"]) * 255.0 / 65535.0
    high = int(frames[3].props["AnaRangeMax"]) * 255.0 / 65535.0
    dark_ratio = float(frames[4].props["AnaDarkAverage"])
    bright_ratio = float(frames[5].props["AnaBrightAverage"])
    mean_square = float(frames[6].props["AnaSquareAverage"])
    contrast = math.sqrt(max(0.0, mean_square - (mean_lum / 255.0) ** 2)) * 255.0

    if dark_ratio > 0.7 and mean_lum < 40.0:
        level = b"extreme"
    elif dark_ratio > 0.5 or mean_lum < 60.0:
        level = b"severe"
    elif dark_ratio > 0.3 or mean_lum < 90.0:
        level = b"moderate"
    else:
        level = b"mild"

    output.props["EnhMeanLum"] = mean_lum
    output.props["EnhMedianLum"] = median_lum
    output.props["EnhDarkRatio"] = dark_ratio
    output.props["EnhBrightRatio"] = bright_ratio
    output.props["EnhContrast"] = contrast
    output.props["EnhDynRange"] = high - low
    output.props["EnhLevel"] = level
    return output


def analyze_image(
    clip: vs.VideoNode, cfg: ColorConfig = DEFAULT_COLOR
) -> vs.VideoNode:
    """Attach the original analyzer's per-frame metrics as frame properties.

    Added properties are EnhMeanLum, EnhMedianLum, EnhDarkRatio,
    EnhBrightRatio, EnhContrast, EnhDynRange and EnhLevel. This is the lazy
    VapourSynth equivalent of returning one Python dictionary per input image.
    """
    luma = _luma16(clip, cfg)
    plane_minmax = _plugin_function("vszip", "PlaneMinMax")
    mean_props = core.std.PlaneStats(luma, prop="AnaMean")
    median_props = plane_minmax(
        luma, minthr=0.5, planes=[0], prop="AnaMedian"
    )
    range_props = plane_minmax(
        luma, minthr=0.05, maxthr=0.05, planes=[0], prop="AnaRange"
    )

    dark_threshold = 50.0 / 255.0 * 65535.0
    bright_threshold = 200.0 / 255.0 * 65535.0
    dark_mask = core.std.Expr(
        luma, expr=[f"x {dark_threshold:.12g} < 65535 0 ?"]
    )
    bright_mask = core.std.Expr(
        luma, expr=[f"x {bright_threshold:.12g} > 65535 0 ?"]
    )
    dark_props = core.std.PlaneStats(dark_mask, prop="AnaDark")
    bright_props = core.std.PlaneStats(bright_mask, prop="AnaBright")

    luma_float = core.resize.Point(luma, format=vs.GRAYS)
    squared = core.std.Expr(luma_float, expr=["x dup *"])
    square_props = core.std.PlaneStats(squared, prop="AnaSquare")

    return core.std.ModifyFrame(
        clip,
        clips=[
            clip,
            mean_props,
            median_props,
            range_props,
            dark_props,
            bright_props,
            square_props,
        ],
        selector=_attach_analysis_props,
    )


def _analysis_clips(
    clip: vs.VideoNode, cfg: ColorConfig
) -> Tuple[vs.VideoNode, vs.VideoNode]:
    luma = _luma16(clip, cfg)
    mean_props = core.std.PlaneStats(luma, prop="Lum")
    threshold = 50.0 / 255.0 * 65535.0
    dark_mask = core.std.Expr(luma, expr=[f"x {threshold:.12g} < 65535 0 ?"])
    dark_props = core.std.PlaneStats(dark_mask, prop="Dark")
    return mean_props, dark_props


def _auto_select(
    n: int,
    frames: Sequence[vs.VideoFrame],
    extreme: vs.VideoNode,
    severe: vs.VideoNode,
    moderate: Sequence[Tuple[float, vs.VideoNode]],
    mild_dim: vs.VideoNode,
    mild: vs.VideoNode,
) -> vs.VideoNode:
    del n
    mean_lum = float(frames[0].props["LumAverage"]) * 255.0
    dark_ratio = float(frames[1].props["DarkAverage"])

    if dark_ratio > 0.7 and mean_lum < 40.0:
        return extreme
    if dark_ratio > 0.5 or mean_lum < 60.0:
        return severe
    if dark_ratio > 0.3 or mean_lum < 90.0:
        return min(moderate, key=lambda item: abs(item[0] - dark_ratio))[1]
    return mild_dim if mean_lum < 100.0 else mild


def enhance_auto(
    clip: vs.VideoNode, cfg: ColorConfig = DEFAULT_COLOR
) -> vs.VideoNode:
    """Select an enhancement branch per frame from mean luma and dark ratio."""
    _validate_clip(clip)
    balanced = auto_white_balance(clip, cfg=cfg)

    extreme = enhance_msrcp(
        balanced,
        scales=(8.0, 50.0, 150.0),
        lower_thr=0.0,
        upper_thr=0.0,
        cfg=cfg,
    )
    extreme = enhance_clahe(extreme, clip_limit=3.0, cfg=cfg)
    extreme = enhance_gamma(extreme, gamma=0.35, cfg=cfg)

    severe = enhance_clahe(balanced, clip_limit=2.5, cfg=cfg)
    severe = enhance_gamma(severe, gamma=0.45, cfg=cfg)
    severe = _unsharp(severe, sigma=2.5, amount=0.4, cfg=cfg)

    # The OpenCV version varies these two parameters continuously with
    # dark_ratio. Prebuilding close samples avoids constructing graph nodes
    # inside FrameEval while retaining per-frame adaptation.
    moderate = []
    for ratio in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5):
        branch = enhance_clahe(
            balanced, clip_limit=1.5 + ratio, tile_grid=(8, 8), cfg=cfg
        )
        branch = enhance_gamma(branch, gamma=0.55 + ratio * 0.2, cfg=cfg)
        branch = _unsharp(branch, sigma=2.0, amount=0.3, cfg=cfg)
        moderate.append((ratio, branch))

    mild = enhance_clahe(balanced, clip_limit=1.2, cfg=cfg)
    mild_dim = enhance_gamma(mild, gamma=0.75, cfg=cfg)

    mean_props, dark_props = _analysis_clips(clip, cfg)
    candidates = [extreme, severe]
    candidates.extend(branch for _, branch in moderate)
    candidates.extend((mild_dim, mild))
    return core.std.FrameEval(
        clip,
        functools.partial(
            _auto_select,
            extreme=extreme,
            severe=severe,
            moderate=moderate,
            mild_dim=mild_dim,
            mild=mild,
        ),
        prop_src=[mean_props, dark_props],
        clip_src=candidates,
    )


METHODS: Dict[str, Callable[..., vs.VideoNode]] = {
    "auto": enhance_auto,
    "comprehensive": enhance_comprehensive,
    "strong": enhance_strong,
    "clahe": enhance_clahe,
    "msrcp": enhance_msrcp,
    "gamma": enhance_gamma,
    "dehaze": enhance_dehaze,
    "auto_levels": enhance_auto_levels,
    "ssr": enhance_ssr,
    "white_balance": auto_white_balance,
    "perfect_reflector": auto_white_balance_perfect_reflector,
    "brightness_contrast": enhance_brightness_contrast,
}


def enhance_image(
    clip: vs.VideoNode,
    method: str = "comprehensive",
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Common entry point; the output keeps the input format and dimensions."""
    try:
        function = METHODS[method]
    except KeyError as exc:
        raise ValueError(
            f"unknown method {method!r}; available methods: {', '.join(METHODS)}"
        ) from exc
    return function(clip, cfg=cfg)


def enhance_video_frame(
    clip: vs.VideoNode,
    method: str = "comprehensive",
    cfg: ColorConfig = DEFAULT_COLOR,
) -> vs.VideoNode:
    """Compatibility alias: VapourSynth processing is clip-based and lazy."""
    return enhance_image(clip, method=method, cfg=cfg)


# Executable .vpy configuration ------------------------------------------------
# Full-range RGB/image sources are handled as RGB automatically. For YUV input,
# set source_range/matrix/transfer to the real source metadata.
SOURCE = os.environ.get("VS_SOURCE", r"input.mkv")
METHOD = os.environ.get("VS_METHOD", "auto")
COLOR = ColorConfig(
    matrix=os.environ.get("VS_MATRIX", "709"),
    transfer=os.environ.get("VS_TRANSFER", "709"),
    source_range=os.environ.get("VS_RANGE", "limited"),
    chroma_location=os.environ.get("VS_CHROMA_LOCATION", "left"),
)

source_filter = _plugin_function("bs", "VideoSource")
source = source_filter(source=SOURCE)
enhanced = enhance_image(source, method=METHOD, cfg=COLOR)
enhanced.set_output()
