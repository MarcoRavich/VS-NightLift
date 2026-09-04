# NightLift — Low-Light Enhancement System

A low-light image/video enhancement providing various "classic" algorithms.

## Automatic-analysis code refactoring

**Required plugin is `BestSource`.**

**Metrics: 6 → 2**
The decision ladder (`_dark_level`) only ever consumes `mean_luma` and `dark_ratio`. The original also computed median luma, bright ratio, contrast, and dynamic range — none of which feed the pipeline suggestion, they were purely informational in the JSON. 

**Simplyfied frames sampling**
The anchor/window/balanced-count machinery (`_balanced_counts`, `_window_bounds`, `sample_percentages`) added a lot of code to solve a problem that a simple evenly-spaced sweep solves just as well for a statistical median. New version: 5–12 samples (evenly spaced across 15–85%) scaling gently with duration, capped at 12 instead of 30 — that cap is the single biggest execution-time win, since frame decode (especially through BestSource's seek/keyframe logic) is the actual bottleneck, not the analysis math. Parallel decoding via `get_frame_async` with ≤12 frames there's no need to throttle requests.

**Bilinear Downscale:** for the analysis proxy — cheaper, and irrelevant to accuracy since you're only reading a scalar mean/ratio, not looking at the pixels.
