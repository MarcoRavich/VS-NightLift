# NightLift — Low-Light Enhancement System

A low-light image/video enhancement web application based on **Flask + OpenCV**, implemented purely with OpenCV, providing various classic enhancement algorithms.

## Features

- 🖼️ **Image Enhancement** — Upload low-light images and enhance them with one click.

- 🎬 **Video Enhancement** — Upload low-light videos, enhance them frame-by-frame, and then composite the output.

- 🔍 **Automatic Analysis** — Analyzes image brightness, contrast, dark pixel ratio, and other indicators to automatically determine the darkness level.

- 🤖 **Auto Mode** — Adapts to parameter selection, automatically choosing the optimal enhancement strategy based on image darkness.

- 📊 **Multiple Algorithms** — Built-in 8 classic enhancement algorithms + 3 combined pipelines.

- 🔎 **Lightbox Preview** — Click on the image to zoom in for full-screen viewing; semi-transparent mask + blurred background.

- ⌨️ **Keyboard Operation** — Use the ← → keys to switch between the original and enhanced images in the lightbox; ESC closes the lightbox.

- 📐 **Side by Side Comparison** — Compare the original image and the enhanced effect side-by-side.

## Enhancement Methods

| Methods | Description |
|------|------|
| **Auto** (One-Click Enhancement) | Automatically analyzes darkness and adaptively selects the best parameters |
| **CLAHE** | Contrast Limited Adaptive Histogram Equalization for the L channel in the LAB color space |
| **Gamma** | Gamma correction, non-linear brightness enhancement |
| **Auto Levels** | Automatic level stretching, histogram stretching to [0, 255] |
| **MSRCP** | Multi-Scale Retinex with Chromaticity Preservation |
| **SSR** | Single-Scale Retinex |
| **Dehaze** | Dark Channel Prior |
| **White Balance** | Gray World / Perfect Reflector |
| **Brightness/Contrast** | Adaptive brightness/contrast adjustment |
| **Comprehensive** | Combined pipeline: Auto White Balance + CLAHE + Gamma + Unsharp Masking Sharpening |
| **Strong** | Powerful Pipeline: MSRCP + CLAHE + Gamma, for extreme low light |

## Darkness Analysis

The system will automatically analyze the following metrics of the uploaded image:

- **Mean Luminance** — Average Luminance

- **Median Luminance** — Median Luminance

- **Dark Ratio** — Dark pixel ratio (< 50)

- **RMS Contrast** — Root Mean Square Contrast

- **Dynamic Range** — Dynamic Range (P95 - P5)

Based on the analysis results, the image is divided into four darkness levels:

| Level | Judgment Criteria | Auto Strategy |

|------|----------|-----------|

| **extreme** (Extremely Dark) | dark_ratio > 0.7 and mean_lum < 40 | MSRCP + Strong CLAHE + Strong Gamma |

| **severe** (Very Dark) | dark_ratio > 0.5 or mean_lum < 60 | White Balance + Medium Clahe + Medium Gamma + Sharpening |

| **moderate** (darker) | dark_ratio > 0.3 or mean_lum < 90 | White Balance + Light Clahe + Light Gamma |

| **mild** (slightly dark) | Other cases | White Balance + Light Clahe only |

## Algorithm References

- **CLAHE**: Zuiderveld, K. "Contrast Limited Adaptive Histogram Equalization." Graphics Gems IV, 1994.

- **MSRCP**: Jobson, D. J., et al. "A Multiscale Retinex for Bridging the Gap Between Color Images and the Human Observation of Scenes." IEEE TIP, 1997.

- **Dark Channel Prior**: He, K., et al. "Single Image Haze Removal Using Dark Channel Prior." CVPR, 2009.

- **Gray World**: Buchsbaum, G. "A Spatial Processor Model for Object Color Perception." J. Franklin Institute, 1980.

## License

MIT
