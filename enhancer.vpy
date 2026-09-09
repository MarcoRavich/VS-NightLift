# Adaopted from Audion VS Engine preset: https://github.com/Tensionix/vs-engine-lite/blob/main/system_core/presets/restoration/dehaze_local_contrast.vpy
# 
# Stack:  Local-contrast enhancement via a smooth, anti-aliased low-pass
#         (Bicubic B-spline downscale/upscale -- cheap at any radius, no
#         ringing, unlike a literal large-kernel Gaussian at this scale)
#         plus a luma-zone mask, so the enhancement is strongest on hazed
#         mids and fades out at the bright and dark extremes -- preventing
#         the typical "Dehaze in Resolve" artefact where highlights blow
#         out and shadows go muddy.
#
#         What this gives you that Adobe / DaVinci do NOT:
#         - per-zone strength (shadow / mid / highlight) baked in
#         - no color shift (luma-only operation; chroma untouched)
#         - works on 16-bit YUV (no 8-bit posterization)
#         - reproducible from the command line, easy to batch
#
# Changes from the found version (borrowing NightLift's approach/code):
#   - All math now runs in a canonical FULL-RANGE 16-bit domain, explicitly
#     converted from/to the source's real range. The original hardcoded
#     mid=32768 / peak=65535 directly on (typically limited-range) delivered
#     video, which quietly misplaced the mask's fade-out points and the
#     clamp ceiling by the footroom/headroom amount.
#   - fmtc.bitdepth (external plugin) replaced with native resize.Point
#     format casts, via VideoFormat.replace() -- one less required
#     dependency, same cost, and keeps whatever chroma subsampling the
#     source actually has instead of assuming one.
#   - Bilinear downscale replaced with Bicubic B-spline (b=1, c=0). At
#     RADIUS=16 on a 1080-line source that's a ~32x downscale, and Bilinear
#     has poor stopband rejection at that ratio -- it aliases, which then
#     reads as blockiness in the "low-pass" instead of smooth haze removal.
#     B-spline is a true smoothing kernel with no ringing, same cost.
#   - The high-pass, the zone mask, and the blend -- previously three
#     filter passes (Expr + Expr + MaskedMerge) -- are fused into one Expr.
#   - matrix/transfer/primaries/range are read from env vars and drive
#     BOTH the internal math and the output stamp via resize's string API
#     (matrix_s=/transfer_s=/primaries_s=/range_s=), so they can no longer
#     silently disagree, and so the stamp itself doesn't depend on which
#     raw _ColorRange/_Matrix integer convention the running VapourSynth
#     build happens to use (that numbering has changed across versions).
#   - akarin's JIT Expr is used automatically when installed.
#
# Required:
#   VS_INPUT     absolute path to source media
# Optional:
#   VS_STRENGTH  float 0.0..2.0 -- overall amount of local contrast added
#                back. 0 = passthrough, 1.0 = sane default, 2.0 = aggressive.
#                                                                  default 1.0
#   VS_RADIUS    int 1..16 -- size of the low-pass kernel in pixels; bigger
#                = enhances bigger structures (haze removal), smaller =
#                enhances fine detail (clarity).                    default 8
#   VS_MATRIX    matrix / transfer / primaries, as resize's "_s" string
#   VS_TRANSFER  names (e.g. "709", "601", "170m"). Only used to correctly
#   VS_PRIMARIES stamp the output -- this script never converts color
#                space, so a wrong value here mislabels the result but
#                doesn't corrupt it.                              default "709"
#   VS_RANGE     "limited" or "full" -- the source's ACTUAL pixel range.
#                Get this wrong and the mask's fade-out points, and the
#                unsharp clamp, are off by the footroom/headroom amount.
#                                                            default "limited"
#
import os
import vapoursynth as vs

core = vs.core
# akarin's Expr is JIT-compiled and noticeably faster than std.Expr's
# interpreter for an expression this size; fall back cleanly when absent.
_EXPR = core.akarin.Expr if hasattr(core, "akarin") else core.std.Expr

INPUT        = os.environ["VS_INPUT"]
STRENGTH     = max(0.0, min(2.0, float(os.environ.get("VS_STRENGTH", "1.0"))))
RADIUS       = max(1,   min(16,  int(os.environ.get("VS_RADIUS",   "8"))))
MATRIX       = os.environ.get("VS_MATRIX", "709")
TRANSFER     = os.environ.get("VS_TRANSFER", "709")
PRIMARIES    = os.environ.get("VS_PRIMARIES", "709")
SOURCE_RANGE = os.environ.get("VS_RANGE", "limited")


def _load(path: str):
    try:
        return core.lsmas.LWLibavSource(path)
    except (vs.Error, AttributeError):
        return core.ffms2.Source(path)


src = _load(INPUT)

# 16-bit for clean math. Native format cast (no external plugin needed) --
# VideoFormat.replace() keeps whatever chroma subsampling the source has.
clip = core.resize.Point(src, format=src.format.replace(bits_per_sample=16).id)

# Pull the luma plane and canonicalize it to full range. Everything below
# assumes 0 = black, 65535 = white, 32768 = mid grey -- only true if we
# make it true, which the original version never did.
luma_native = core.std.ShufflePlanes(clip, 0, vs.GRAY)
luma = core.resize.Point(luma_native, format=vs.GRAY16, range_in_s=SOURCE_RANGE, range_s="full")

# Low-pass via downscale -> upscale with a B-spline (b=1, c=0) kernel: a
# true smoothing filter with no ringing, and -- unlike a literal large-
# radius convolution applied at full resolution -- its cost barely grows
# with RADIUS, since the heavy lifting happens on a tiny intermediate image.
w, h = luma.width, luma.height
small_w = max(8, w // (RADIUS * 2))
small_h = max(8, h // (RADIUS * 2))
low = core.resize.Bicubic(luma, width=small_w, height=small_h, filter_param_a=1, filter_param_b=0)
low = core.resize.Bicubic(low,  width=w,       height=h,       filter_param_a=1, filter_param_b=0)

# Fused high-pass + zone mask + blend in a single Expr pass (was
# Expr + Expr + MaskedMerge -- three full-frame passes down to one).
#   mask(x) = clamp(1 - ((x - 32768) / 32768)^2, 0, 1)   -- peaks at mid
#             grey, fades to 0 at black/white
#   boosted = x + mask(x) * STRENGTH * (x - low)
# Note: this clamps once at the end rather than clamping the raw boost and
# then blending it by mask -- the two differ only where the boost would
# clip, which is exactly where the mask already suppresses it toward 0, so
# the difference is negligible in practice.
expr = (
    f"x y - {STRENGTH} * "
    "x 32768 - 32768 / dup * 1 swap - 0 max 1 min "
    "* x + 0 max 65535 min"
)
boosted_full = _EXPR([luma, low], [expr])

# Back to the source's native range before recombining with chroma.
boosted = core.resize.Point(boosted_full, format=vs.GRAY16, range_in_s="full", range_s=SOURCE_RANGE)

# Recombine with original chroma (never touched -- no color shift).
out = core.std.ShufflePlanes([boosted, clip, clip], [0, 1, 2], vs.YUV)

# Output in 10-bit for the standard encoder profiles, and stamp the real
# color metadata in the same call -- native cast + relabel, no fmtc, no
# hand-rolled integer color-property codes.
out = core.resize.Point(
    out, format=out.format.replace(bits_per_sample=10).id,
    matrix_s=MATRIX, transfer_s=TRANSFER, primaries_s=PRIMARIES, range_s=SOURCE_RANGE,
)
out.set_output()
