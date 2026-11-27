# FTE3600 Offline Multi-Algorithm Matcher Benchmark

`tools/fte3600-bench/` is a self-contained offline benchmark lab for evaluating
`64x80` fingerprint matchers for the FocalTech FTE3600 family (primarily FW9369
`0x9369` `64x80` captures, plus supported FTE3600 sensor geometries).

It compiles and links directly against the repository's real
[`sam7-brisk`](../../libfprint/drivers/fte3600/sam7-brisk/libfprint/matchers/brisk/brisk.c)
C core and driver adapter (`fte3600-brisk.c`) with the same strict IEEE
floating-point flags (`-DFTE3600_ENABLE_PERSONAL_AUTH=1 -fno-fast-math
-ffp-contract=off -fexcess-precision=standard -frounding-math`), and runs inside
Docker so OpenCV does not need to be installed on the host.

## Matchers Compared

1. **`BRISK-baseline`** — Unmodified `sam7-brisk` C extractor and matcher via
   [`brisk_cli.c`](../../tools/fte3600-bench/brisk_cli.c).
2. **`CLAHE+BRISK`** — Same `sam7-brisk` C core preceded by deterministic 4x4
   tile 2D CLAHE (`clip_factor=4.0`) with a flat-frame contrast guard
   (`max - min < 8`).
3. **`CLAHE+AKAZE`** *(Docker / OpenCV)* — 4x4 2D CLAHE, 2x bicubic upscale,
   OpenCV AKAZE (`threshold=0.0004`), cross-check Hamming `BFMatcher`, and
   `cv2.estimateAffinePartial2D(..., RANSAC)` geometric verification (`0.85 <= scale <= 1.15`).
4. **`CLAHE+SIGFM(SIFT)`** *(Docker / OpenCV)* — 4x4 2D CLAHE, 2x bicubic
   upscale, OpenCV SIFT (`contrastThreshold=0.02`, `edgeThreshold=15`), Lowe's
   ratio test (`0.78`), and `cv2.estimateAffinePartial2D(..., RANSAC)` geometric
   verification (modeled after community libfprint SIGFM).

## Collecting Real `64x80` `.pgm` Frames via `FTE3600_DUMP_DIR`

As implemented in
[`libfprint/drivers/fte3600/fte3600.c`](../../libfprint/drivers/fte3600/fte3600.c#L200-L223),
setting `FTE3600_DUMP_DIR` causes every accepted sensor capture to be written as
an 8-bit binary PGM (`P5`) file (`frame-<sec>-<seq>-<uuid>.pgm`).

> [!WARNING]
> Frame dumps contain raw biometric ridge images. Keep `FTE3600_DUMP_DIR`
> private (`0700`), never commit `.pgm` captures to git, and delete temporary
> captures once benchmarking is finished.

To collect captures from separate fingers cleanly for offline evaluation:

```sh
# Create one directory per finger / identity (or genuine/ + impostor/)
mkdir -p ~/fte3600-captures/right_index ~/fte3600-captures/right_middle

# Collect ~12-16 touches per finger using fprintd or toolkit/verify tools:
sudo env FTE3600_DUMP_DIR=~/fte3600-captures/right_index \
  fprintd-enroll -f right-index-finger "$USER"
```

Two dataset directory layouts are auto-detected by `bench.py --dataset`:

- **Multi-finger layout** (`<finger_id>/*.pgm`): every distinct subdirectory is
  treated as a separate finger identity; intra-directory pairs are genuine,
  cross-directory pairs are impostor.
- **Genuine / Impostor layout** (`genuine/*.pgm` and `impostor/*.pgm`, or
  nested finger subdirectories underneath): pairs within `genuine/` (matching
  identity) are genuine, and cross-pairs between `genuine/` and `impostor/` are
  impostor.

## Running the Benchmark

### Full 4-Matcher Run in Docker (No Host OpenCV Required)

```sh
# 1. Synthetic stress benchmark (default: 10 fingers x 16 captures = 160 frames)
./tools/fte3600-bench/run-docker.sh

# 2. Real FTE3600_DUMP_DIR dataset (mounted read-only at /data inside container)
./tools/fte3600-bench/run-docker.sh ~/fte3600-captures

# Save Markdown and JSON reports on the host:
./tools/fte3600-bench/run-docker.sh --synthetic > bench-report.md
```

### Host BRISK-Only Run (Without Docker / OpenCV)

Requires only `gcc`, `pkg-config`, `glib-2.0`, `python3`, and `numpy`:

```sh
python3 tools/fte3600-bench/bench.py --synthetic --brisk-only
```

If `brisk_cli` is not already compiled, `bench.py` compiles it into a
temporary directory automatically and cleans it up on exit.

## Measured Results (Synthetic Ridge Stress Suite)

Configuration: `docker run --rm fte3600-bench:latest --synthetic`
(`10 fingers × 16 captures = 160 frames`, `seed=36002026`, `1,200 genuine pairs`,
`11,520 impostor pairs`; captures `0..7` form the 8-sample gallery, `0..11` form
the 12-sample gallery, and `12..15` are held-out verification probes).

All four matchers below were measured end-to-end inside the `fte3600-bench`
Docker container (`python:3.12-slim` + `opencv-python-headless<5` + compiled
`brisk_cli` linking the repository's real `sam7-brisk` C core):

### 1. Pairwise Verification ROC & Latency

| Matcher | TAR @ FAR=0% | TAR @ FAR<=1% | EER | Gate TAR / FAR | Mean Match (ms) | Mean Extract (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `BRISK-baseline` | 34.58% | 41.08% | 32.16% | 32.75% / 0.00% | 0.429 | 15.184 |
| `CLAHE+BRISK` | 38.92% | 47.67% | 18.99% | 36.83% / 0.00% | 0.594 | 17.491 |
| `CLAHE+AKAZE` | 74.67% | 88.42% | 4.84% | 88.50% / 1.22% | 0.385 | 2.062 |
| `CLAHE+SIGFM(SIFT)` | 79.58% | 94.67% | 3.58% | 96.92% / 4.49% | 0.626 | 7.113 |

### 2. Multi-Sample Template Gallery (8-Sample vs 12-Sample, TAR @ FAR=0%)

| Matcher | 8-Sample TAR @ FAR=0% | 12-Sample TAR @ FAR=0% | 8-Sample Gate TAR / FAR | 12-Sample Gate TAR / FAR |
| --- | ---: | ---: | ---: | ---: |
| `BRISK-baseline` | 72.50% | 72.50% | 67.50% / 0.00% | 70.00% / 0.00% |
| `CLAHE+BRISK` | 77.50% | 80.00% | 75.00% / 0.00% | 77.50% / 0.00% |
| `CLAHE+AKAZE` | 100.00% | 100.00% | 100.00% / 6.67% | 100.00% / 11.11% |
| `CLAHE+SIGFM(SIFT)` | 95.00% | 97.50% | 100.00% / 23.06% | 100.00% / 34.44% |

### 3. Simulated Enrollment Stage Acceptance (8-Sample Target)

| Matcher | Strict Touch Accept | Strict Enroll Complete | Two-Tier Touch Accept | Two-Tier Enroll Complete |
| --- | ---: | ---: | ---: | ---: |
| `BRISK-baseline` | 80.00% | 100.00% | 80.00% | 100.00% |
| `CLAHE+BRISK` | 89.89% | 100.00% | 89.89% | 100.00% |
| `CLAHE+AKAZE` | 100.00% | 100.00% | 100.00% | 100.00% |
| `CLAHE+SIGFM(SIFT)` | 100.00% | 100.00% | 100.00% | 100.00% |

### Takeaways

- **Hardware validation finding (why the production driver keeps single-pass LCN)**:
  Although stacking an external 2D CLAHE pass ahead of `sam7-brisk` improved
  metrics on clean synthetic ridges (`EER 32.16% -> 18.99%`), live testing on a
  physical GPD Pocket 3 (`FW9369`, `64x80`) showed that external CLAHE combined
  with `sam7-brisk`'s built-in 13×13 Local Contrast Normalization creates a
  double-LCN pass that amplifies real capacitive sensor floor noise into
  109–151 unstable keypoints (`mutual 0`). The production driver therefore
  keeps `sam7-brisk`'s native single-pass LCN unchanged, expands the gallery to
  12 samples, and enables coverage enrollment for `<= 64x80` small press arrays
  (confirmed on real `FW9369` hardware: 12/12 zero-retry enrollment in ~13 s,
  strict verification unchanged with `inliers 9–11`, `hamming 20.0–22.2`,
  `rms 0.32–0.37` on matches).
- **Future matcher direction (`CLAHE+AKAZE` vs `CLAHE+SIGFM`)**: At calibrated
  `FAR=0%` ROC operating points, `CLAHE+AKAZE` reaches `100.00%` gallery TAR
  (`4.84%` pairwise EER) with `2.062 ms` extraction time—faster than both BRISK
  and SIFT. However, the uncalibrated fixed-threshold Gate columns (`6.67%`–`34.44%`
  multi-template FAR) show why an optional OpenCV matcher must only be introduced
  after threshold calibration on real physical `FTE3600_DUMP_DIR` captures.

## Scope & Interpretation Limits

> [!IMPORTANT]
> **Synthetic stress-test results are NOT population FAR/FRR claims.**
> The synthetic suite uses procedural `64x80` ridge patterns with deterministic
> perturbations (uneven illumination/pressure gradients, dry low-contrast
> frames with variance below `64.0`, `±15°` rotation, `±8 px` translation,
> elastic warp, and sensor readout noise) specifically to compare relative
> matcher behavior and preprocessing gains. It does not model real human skin
> moisture variability, scarring, partial lifts, or population-scale biometric
> entropy. As documented in
> [`fte3600-brisk.h`](../../libfprint/drivers/fte3600/sam7-brisk/libfprint/drivers/fte3600-brisk.h#L62-L88)
> and [`auth.c`](../../libfprint/drivers/fte3600/auth.c#L1-L4), the driver's
> BRISK policy remains uncalibrated (`FTE3600_BRISK_THRESHOLDS_CALIBRATED == 0`)
> and is intended for personal testing unless evaluated on a real multi-subject
> physical capture corpus via `--dataset`.
