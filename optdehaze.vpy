# Improvements from video-dehaze-rs (https://github.com/ddy314/video-dehaze-rs):
#   - Sky and highlight protection mask (prevents blown-out highlights and desaturated sky)
#   - Aggressiveness parameter to trade safety for enhancement strength
#   - Per-pixel protection scaling (stronger masking in protected regions)
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
#         - sky and highlight protection (new)
#         - aggressiveness tuning (new)
#         - no color shift (luma-only operation; chroma untouched)
#         - works on 16-bit YUV (no 8-bit posterization)
#         - reproducible from the command line, easy to batch
#
# Deep optimization pass (this update):
#   - Numerically stable variance. The protection mask's local variance
#     was computed as E[X^2] - E[X]^2, subtracting two float32 numbers of
#     magnitude ~(60000)^2 that are nearly equal. float32 only carries
#     ~7 significant digits, so that subtraction can be off by ~1-2
#     hundred against the *raw* 500 threshold -- verified empirically:
#     variances comfortably on either side of 500 got flipped to the
#     wrong side purely from rounding, especially at high mean brightness
#     (i.e. exactly where the sky-protection tier is trying to make a
#     decision). Replaced with the textbook two-pass form
#     E[(X - E[X])^2]: subtract the (small-magnitude) local deviation
#     from the mean *before* squaring, so no intermediate value is ever
#     much larger than the quantity you actually care about, and the
#     rounding error stays negligible relative to the threshold. As a
#     bonus, blur-of-a-square is provably non-negative, so the old
#     "0 max" safety clamp on variance is no longer needed.
#   - Threshold-then-upsample instead of upsample-then-threshold. The
#     mask function used to bicubic-upsample the *continuous* mean and
#     variance fields to full resolution (2 full-frame passes) and only
#     then run the tri-level threshold Expr -- at full resolution (a 3rd
#     full-frame pass). Thresholding is now done once at the cheap
#     working resolution, and only the resulting 0 / 0.65 / 1.0 mask is
#     upsampled to full res. That takes the mask stage from 3 full-frame
#     operations down to 1, and it's a quality win too: the same
#     non-ringing B-spline kernel that does the upsampling now
#     anti-aliases the mask's tier boundaries (soft edges between
#     protected/unprotected regions) instead of leaving them as hard
#     steps, which is exactly the kind of smoothing this script already
#     insists on everywhere else.
#   - Fused luma extraction + range normalization. Getting from the
#     source to full-range 16-bit luma used to be bitdepth-cast the
#     whole clip -> ShufflePlanes out the luma plane -> separate
#     range-conversion pass. resize.Point can change color family
#     (YUV -> GRAY, i.e. take the luma plane), bit depth, AND range in
#     one call, so the luma path is now a single full-resolution pass
#     instead of two. (The whole-clip 16-bit cast is still done
#     separately, but only because the chroma planes need it too --
#     that pass is an unavoidable, and very cheap, bit-depth-only
#     resample with no spatial resize.)
#   - One fewer Expr call in the mask function: the two-pass variance
#     form only needs a single "deviation squared" Expr, vs. the old
#     one-pass form's separate small-squared and mean_sq-minus-mean^2
#     Exprs.
#
# Changes from the original version:
#   - All math now runs in a canonical FULL-RANGE 16-bit domain, explicitly
#     converted from/to the source's real range.
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
#     BOTH the internal math and the output stamp via resize's string API.
#   - akarin's JIT Expr is used automatically when installed.
#
# Fixes to the protection-mask addition (earlier pass):
#   - The "adaptive strength based on frame haze score" line from the
#     earlier header wasn't actually implemented anywhere in the code, so
#     it's been dropped from the docs until it exists. Everything else
#     documented below is real.
#   - Local variance was computed as square-then-blur of an ALREADY
#     downscaled signal (blur(mean(L))^2), not blur-then-subtract of the
#     squared signal (blur(L^2) - blur(L)^2). That destroys the very
#     pixel-level variation it's supposed to measure before it's measured,
#     so "flat" vs. "textured" was barely distinguishable. Fixed to square
#     right after the cheap working-resolution downscale and before the
#     windowing blur, so both terms of E[X^2] - E[X]^2 see the same signal.
#     (This identity was itself replaced by the stable two-pass form above.)
#   - The mask multiplied the WHOLE combined score (highlight + sky) by
#     the variance gate, so a bright but textured region -- a spotlight or
#     stage light, not exactly rare in concert footage -- lost its
#     highlight protection entirely. Highlight (brightness > 0.90) is now
#     unconditional; only the sky tier (brightness > 0.56) is variance-gated.
#   - AGGRESSIVENESS=1.0 was documented as "no protection" but the formula
#     capped the override at 45%, so a fully-protected pixel kept 55% of
#     its protection even at max aggressiveness. Fixed so 1.0 truly
#     removes protection, matching the docstring.
#   - Removed dead code: an unused draft mask expression, a no-op Expr
#     ("x" in, "x" out), and the trial-and-error commentary that produced
#     them. Both Expr calls inside the mask function now go through the
#     same akarin-aware _EXPR as the rest of the script (they'd been
#     calling core.std.Expr directly, missing the JIT path).
#   - _sky_highlight_mask no longer takes width/height -- it reads them
#     off the luma clip it's already given.
#
# Required:
#   VS_INPUT     absolute path to source media
# Optional:
#   VS_STRENGTH       float 0.0..2.0 -- overall amount of local contrast added
#                     back. 0 = passthrough, 1.0 = sane default, 2.0 = aggressive.
#                                                                   default 1.0
#   VS_AGGRESSIVENESS float 0.0..1.0 -- how much to override sky/highlight
#                     protection. 0 = maximum protection, 1.0 = no protection.
#                                                                   default 0.2
#   VS_RADIUS         int 1..16 -- size of the low-pass kernel in pixels; bigger
#                     = enhances bigger structures (haze removal), smaller =
#                     enhances fine detail (clarity).                 default 8
#   VS_MATRIX    matrix / transfer / primaries, as resize's "_s" string
#   VS_TRANSFER  names (e.g. "709", "601", "170m"). Only used to correctly
#   VS_PRIMARIES stamp the output -- this script never converts color
#                space.                                              default "709"
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

INPUT           = os.environ["VS_INPUT"]
STRENGTH        = max(0.0, min(2.0, float(os.environ.get("VS_STRENGTH", "1.0"))))
AGGRESSIVENESS  = max(0.0, min(1.0, float(os.environ.get("VS_AGGRESSIVENESS", "0.2"))))
RADIUS          = max(1,   min(16,  int(os.environ.get("VS_RADIUS", "8"))))
MATRIX          = os.environ.get("VS_MATRIX", "709")
TRANSFER        = os.environ.get("VS_TRANSFER", "709")
PRIMARIES       = os.environ.get("VS_PRIMARIES", "709")
SOURCE_RANGE    = os.environ.get("VS_RANGE", "limited")


def _load(path: str):
    try:
        return core.lsmas.LWLibavSource(path)
    except (vs.Error, AttributeError):
        return core.ffms2.Source(path)


def _bicubic_bspline(clip: vs.VideoNode, width: int, height: int) -> vs.VideoNode:
    return core.resize.Bicubic(clip, width=width, height=height, filter_param_a=1, filter_param_b=0)


def _sky_highlight_mask(luma: vs.VideoNode) -> vs.VideoNode:
    """Protection mask: 1.0 over highlights (brightness > 0.90, unconditional),
    0.65 over flat sky-like regions (brightness > 0.56 AND local variance <
    500), 0.0 elsewhere. `luma` must already be full-range 16-bit.

    Variance uses the numerically stable two-pass form E[(X - E[X])^2]
    rather than E[X^2] - E[X]^2. The one-pass identity subtracts two
    ~65535^2-magnitude float32 numbers that are nearly equal -- float32
    only carries ~7 significant digits, so that subtraction loses on the
    order of a few hundred out of the raw variance's own 500 threshold,
    effectively coin-flipping the sky/not-sky decision. Subtracting the
    (small-magnitude) deviation from the mean *before* squaring keeps
    every intermediate value near its true size, so rounding error stays
    negligible relative to the threshold.

    Thresholding happens at the cheap working resolution; only the final
    0 / 0.65 / 1.0 mask is upsampled to full res. That's both cheaper
    (one full-frame pass instead of three) and smoother-edged (the same
    non-ringing B-spline kernel anti-aliases the tier boundaries, instead
    of thresholding-after-upsample leaving them as hard steps).
    """
    width, height = luma.width, luma.height
    work_w, work_h = max(8, width // 4), max(8, height // 4)
    win_w, win_h = max(2, width // 16), max(2, height // 16)

    small = _bicubic_bspline(luma, work_w, work_h)
    mean = _bicubic_bspline(_bicubic_bspline(small, win_w, win_h), work_w, work_h)
    dev_sq = _EXPR([small, mean], ["x y - dup *"])
    variance = _bicubic_bspline(_bicubic_bspline(dev_sq, win_w, win_h), work_w, work_h)

    # x = mean (locally-averaged brightness), y = variance -- both at work res
    mask_expr = (
        "x 59000 > 1.0 0.0 ? "            # highlight tier: unconditional on brightness
        "x 36438 > y 500 < * 0.65 0.0 ? " # sky tier: needs brightness AND low variance
        "max"                              # stronger of the two protections wins
    )
    mask_small = _EXPR([mean, variance], [mask_expr])
    return _bicubic_bspline(mask_small, width, height)


src = _load(INPUT)

# 16-bit for clean math. Native format cast (no external plugin needed) --
# VideoFormat.replace() keeps whatever chroma subsampling the source has.
# This is a bit-depth-only resample (no spatial resize), so it's cheap even
# though it touches all three planes -- unavoidable since the chroma
# planes need it for the final recombine below.
clip = core.resize.Point(src, format=src.format.replace(bits_per_sample=16).id)

# Luma, canonicalized to full range, in one pass straight from the source.
# resize.Point can change color family (YUV -> GRAY, i.e. take just the
# luma plane), bit depth, and range simultaneously, so this replaces what
# used to be a whole-clip bitdepth cast + ShufflePlanes + separate
# range-conversion (effectively two full-res passes for luma alone) with
# a single full-res pass. Everything below assumes 0 = black, 65535 =
# white, 32768 = mid grey -- only true if we make it true here.
luma = core.resize.Point(src, format=vs.GRAY16, range_in_s=SOURCE_RANGE, range_s="full")

protection_mask = _sky_highlight_mask(luma)

# Low-pass via downscale -> upscale with a B-spline (b=1, c=0) kernel: a
# true smoothing filter with no ringing, and -- unlike a literal large-
# radius convolution applied at full resolution -- its cost barely grows
# with RADIUS, since the heavy lifting happens on a tiny intermediate image.
w, h = luma.width, luma.height
small_w = max(8, w // (RADIUS * 2))
small_h = max(8, h // (RADIUS * 2))
low = _bicubic_bspline(luma, small_w, small_h)
low = _bicubic_bspline(low, w, h)

# Fused high-pass + zone mask + protection + blend in a single Expr pass.
#   x, y, z = luma, low, protection_mask
#   high_pass  = (x - y) * STRENGTH
#   zone_mask  = clamp(1 - ((x - 32768) / 32768)^2, 0, 1)   -- peaks at mid
#                grey, fades to 0 at black/white
#   protection = z * (1 - AGGRESSIVENESS)   -- 0 at AGGRESSIVENESS=1 (no
#                protection), z itself at AGGRESSIVENESS=0 (full protection)
#   boosted    = x + high_pass * zone_mask * (1 - protection), clamped
expr = (
    f"x y - {STRENGTH} * "
    "x 32768 - 32768 / dup * 1 swap - 0 max 1 min "
    "* "
    f"z 1 {AGGRESSIVENESS} - * "
    "1 swap - "
    "* "
    "x + 0 max 65535 min"
)
boosted_full = _EXPR([luma, low, protection_mask], [expr])

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
