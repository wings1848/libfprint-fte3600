#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Offline multi-algorithm fingerprint matcher benchmark for FocalTech FTE3600 (64x80).

Evaluates:
  1. BRISK-baseline     (real repo sam7-brisk C core via brisk_cli)
  2. CLAHE+BRISK        (real repo sam7-brisk C core + 4x4 2D CLAHE)
  3. CLAHE+AKAZE        (OpenCV AKAZE + cross-check Hamming + RANSAC partial-2D)
  4. CLAHE+SIGFM(SIFT)  (OpenCV SIFT + Lowe ratio test + RANSAC partial-2D)

Supports both deterministic synthetic ridge stress evaluation (--synthetic) and
real FTE3600_DUMP_DIR 64x80 PGM datasets (--dataset /path/to/dump_dir).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

try:
    import cv2  # type: ignore
    HAS_CV2 = True
except ImportError:
    cv2 = None  # type: ignore
    HAS_CV2 = False


@dataclass
class FrameItem:
    finger_id: str
    sample_idx: int
    path: Path
    role: str = "all"  # "all", "genuine", or "impostor"


@dataclass
class PairOutcome:
    genuine: bool
    accepted: bool
    score: float
    inliers: int
    mutual_matches: int
    inlier_ratio: float
    rms_error: float
    mean_hamming: float
    elapsed_ms: float
    extract_ms_a: float
    extract_ms_b: float
    features_a: int
    features_b: int
    self_ok_a: bool
    self_ok_b: bool


def write_pgm(path: Path, img: np.ndarray) -> None:
    h, w = img.shape
    header = f"P5\n{w} {h}\n255\n".encode("ascii")
    path.write_bytes(header + np.ascontiguousarray(img, dtype=np.uint8).tobytes())


def read_pgm(path: Path) -> np.ndarray:
    data = path.read_bytes()
    idx = 0
    n = len(data)
    tokens: List[str] = []
    while len(tokens) < 4:
        while idx < n:
            b = data[idx]
            if chr(b).isspace():
                idx += 1
            elif b == ord("#"):
                while idx < n and data[idx] != ord("\n"):
                    idx += 1
            else:
                break
        if idx >= n:
            raise ValueError(f"Truncated PGM header: {path}")
        start = idx
        while idx < n and not chr(data[idx]).isspace() and data[idx] != ord("#"):
            idx += 1
        tokens.append(data[start:idx].decode("ascii"))
    if tokens[0] != "P5":
        raise ValueError(f"Unsupported PGM magic {tokens[0]!r}: {path}")
    w, h, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
    if maxval != 255:
        raise ValueError(f"Unsupported maxval {maxval}: {path}")
    if idx >= n or not chr(data[idx]).isspace():
        raise ValueError(f"Missing whitespace after PGM header: {path}")
    idx += 1
    expected = w * h
    if n - idx < expected:
        raise ValueError(f"Truncated PGM pixel payload in {path}")
    arr = np.frombuffer(data, dtype=np.uint8, count=expected, offset=idx)
    return arr.reshape((h, w)).copy()


def _bilinear_sample(img: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    h, w = img.shape
    x = np.clip(xs, 0.0, w - 1.0001)
    y = np.clip(ys, 0.0, h - 1.0001)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    wx = x - x0
    wy = y - y0
    v00 = img[y0, x0]
    v01 = img[y0, x1]
    v10 = img[y1, x0]
    v11 = img[y1, x1]
    top = (1.0 - wx) * v00 + wx * v01
    bot = (1.0 - wx) * v10 + wx * v11
    return (1.0 - wy) * top + wy * bot


def _generate_master_finger(rng: np.random.Generator, size: int = 160) -> np.ndarray:
    """Create a 160x160 continuous ridge map with finger-specific topology and minutiae."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    cx = size * 0.5 + rng.uniform(-12.0, 12.0)
    cy = size * 0.5 + rng.uniform(-12.0, 12.0)
    alpha = rng.uniform(-0.85, 0.85)
    ca, sa = math.cos(alpha), math.sin(alpha)
    u = (xx - cx) * ca + (yy - cy) * sa
    v = -(xx - cx) * sa + (yy - cy) * ca

    # 508 DPI ridge wavelength ~8.0..9.6 px -> omega ~ 0.65..0.78 rad/px
    period = rng.uniform(8.0, 9.5)
    omega = 2.0 * math.pi / period
    arch_curv = rng.uniform(-0.012, 0.012)
    arch_amp = rng.uniform(3.0, 9.0)
    arch_freq = rng.uniform(0.025, 0.045)
    phi = omega * (u + arch_curv * (v * v) + arch_amp * np.sin(v * arch_freq))

    # Add topological ridge endings / bifurcations as spiral phase singularities.
    n_minutiae = int(rng.integers(12, 18))
    min_pts: List[Tuple[float, float]] = []
    for _ in range(n_minutiae * 4):
        if len(min_pts) >= n_minutiae:
            break
        mx = float(rng.uniform(34.0, size - 34.0))
        my = float(rng.uniform(30.0, size - 30.0))
        if all((mx - px) ** 2 + (my - py) ** 2 > 14.0 ** 2 for px, py in min_pts):
            min_pts.append((mx, my))
            polarity = 1.0 if rng.random() < 0.5 else -1.0
            phi = phi + polarity * np.arctan2(yy - my, xx - mx)

    # Smooth ridge texture + subtle pore micro-modulation anchored to finger skin.
    ridge = np.tanh(2.1 * np.cos(phi))
    pore_noise = rng.normal(0.0, 1.0, size=(size // 2, size // 2))
    pore_up = np.repeat(np.repeat(pore_noise, 2, axis=0), 2, axis=1)
    ridge = ridge + 0.12 * pore_up
    # Normalize master pattern to [-1, 1]
    ridge = np.clip(ridge / 1.15, -1.0, 1.0)
    return ridge.astype(np.float64)


def generate_synthetic_dataset(
    out_dir: Path,
    n_fingers: int = 10,
    n_captures: int = 16,
    seed: int = 36002026,
) -> List[FrameItem]:
    """Generate deterministic 64x80 synthetic stress frames with realistic degradations."""
    rng = np.random.default_rng(seed)
    w, h = 64, 80
    master_size = 160
    frames: List[FrameItem] = []

    # Stress presets cycled across captures to exercise gallery + held-out probes:
    # (rot_deg, dx, dy, elastic_amp, contrast, bias, grad_amp, noise_std, dry_drop)
    stress_schedule = [
        # 0..7: 8-sample enrollment captures (central cluster +/-3.5px, +/-6deg + 2 dry captures)
        (0.0, 0.0, 0.0, 0.3, 56.0, 132.0, 16.0, 2.8, 0.0),
        (-4.5, -2.5, 1.5, 0.6, 52.0, 126.0, 22.0, 3.2, 0.0),
        (5.0, 2.5, -2.0, 0.7, 48.0, 138.0, 24.0, 3.5, 0.0),
        (-6.0, 1.5, 2.5, 0.8, 9.5, 168.0, 5.0, 2.6, 0.35),     # dry low-contrast (var < 64)
        (6.0, -3.0, -2.0, 0.8, 46.0, 118.0, 26.0, 4.0, 0.0),
        (-3.0, 3.5, 1.0, 0.8, 44.0, 145.0, 28.0, 3.8, 0.0),
        (3.5, -1.5, -3.5, 0.9, 10.5, 96.0, 5.5, 2.8, 0.30),    # dry low-contrast (dark)
        (6.5, 3.0, 3.0, 0.9, 44.0, 130.0, 26.0, 4.0, 0.0),
        # 8..11: extra 4 captures for 12-sample gallery (outer quadrants +/-14deg, +/-7.5px + dry)
        (-13.5, -7.5, 6.5, 1.1, 42.0, 136.0, 30.0, 4.5, 0.0),
        (14.5, 7.5, -7.0, 1.1, 40.0, 122.0, 32.0, 4.5, 0.10),
        (-12.5, 7.5, 6.5, 1.1, 11.0, 178.0, 6.0, 3.0, 0.35),   # dry + outer quadrant
        (13.0, -7.5, -6.5, 1.1, 44.0, 112.0, 30.0, 4.2, 0.0),
        # 12..15: 4 held-out verification probes (outer poses + dry stress)
        (-14.0, -7.5, 7.0, 1.0, 46.0, 130.0, 28.0, 4.0, 0.0),   # outer pose covered by 12-sample
        (14.0, 7.5, -7.0, 1.1, 10.5, 170.0, 5.5, 2.8, 0.30),   # dry + outer pose
        (13.5, -7.5, -6.5, 1.0, 42.0, 115.0, 30.0, 4.2, 0.0),  # outer pose covered by 12-sample
        (-12.0, 7.5, 6.0, 1.1, 12.0, 152.0, 8.0, 3.2, 0.25),   # dry + outer pose
    ]

    gy, gx = np.mgrid[0:h, 0:w].astype(np.float64)
    nx = (gx - (w - 1) * 0.5) / (w * 0.5)
    ny = (gy - (h - 1) * 0.5) / (h * 0.5)
    r2 = nx * nx + ny * ny

    for f_idx in range(n_fingers):
        finger_id = f"finger_{f_idx:02d}"
        f_dir = out_dir / finger_id
        f_dir.mkdir(parents=True, exist_ok=True)
        master = _generate_master_finger(rng, size=master_size)
        mcx = master_size * 0.5
        mcy = master_size * 0.5

        for c_idx in range(n_captures):
            sched = stress_schedule[c_idx % len(stress_schedule)]
            rot_deg, dx, dy, elastic_amp, contrast, bias, grad_amp, noise_std, dry_drop = sched
            # Add small per-finger/capture jitter around schedule within stress bounds
            rot_rad = math.radians(rot_deg + rng.uniform(-1.0, 1.0))
            sx = dx + rng.uniform(-0.6, 0.6)
            sy = dy + rng.uniform(-0.6, 0.6)

            # Mild elastic distortion field
            el_phase_x = rng.uniform(0, 2 * math.pi)
            el_phase_y = rng.uniform(0, 2 * math.pi)
            el_dx = elastic_amp * np.sin(math.pi * nx + el_phase_x) * np.cos(0.7 * math.pi * ny)
            el_dy = elastic_amp * np.sin(math.pi * ny + el_phase_y) * np.cos(0.7 * math.pi * nx)

            # Map sensor coords to master finger coords
            x_centered = gx - (w - 1) * 0.5 + el_dx
            y_centered = gy - (h - 1) * 0.5 + el_dy
            cr, sr = math.cos(rot_rad), math.sin(rot_rad)
            mx = mcx + cr * x_centered - sr * y_centered + sx
            my = mcy + sr * x_centered + cr * y_centered + sy

            ridge_vals = _bilinear_sample(master, mx, my)

            # Uneven illumination / pressure oval
            theta_g = rng.uniform(0.0, 2.0 * math.pi)
            ill_grad = grad_amp * (math.cos(theta_g) * nx + math.sin(theta_g) * ny)
            pressure_mask = np.clip(1.05 - dry_drop * (0.45 * r2 + 0.25 * nx), 0.25, 1.0)
            local_amp = contrast * pressure_mask

            # Sensor noise (Gaussian readout + tiny column FPN)
            col_fpn = rng.normal(0.0, 1.1, size=(1, w))
            pix_noise = rng.normal(0.0, noise_std, size=(h, w))

            frame = bias + ill_grad + local_amp * ridge_vals + col_fpn + pix_noise
            frame_u8 = np.clip(np.rint(frame), 0, 255).astype(np.uint8)

            out_path = f_dir / f"cap_{c_idx:02d}.pgm"
            write_pgm(out_path, frame_u8)
            frames.append(FrameItem(finger_id=finger_id, sample_idx=c_idx, path=out_path))

    return frames


def load_dataset(dataset_dir: Path) -> Tuple[List[FrameItem], str]:
    """Load FTE3600_DUMP_DIR captures from <finger_id>/*.pgm or genuine/+impostor/."""
    if not dataset_dir.is_dir():
        raise FileNotFoundError(f"Dataset directory does not exist: {dataset_dir}")

    genuine_dir = dataset_dir / "genuine"
    impostor_dir = dataset_dir / "impostor"
    frames: List[FrameItem] = []

    if genuine_dir.is_dir() and impostor_dir.is_dir():
        g_files = sorted(genuine_dir.rglob("*.pgm"))
        i_files = sorted(impostor_dir.rglob("*.pgm"))
        if not g_files or not i_files:
            raise ValueError("genuine/ and impostor/ layout requires .pgm files in both")
        for idx, p in enumerate(g_files):
            rel_parent = p.parent.relative_to(genuine_dir).as_posix()
            fid = "genuine" if rel_parent == "." else f"genuine/{rel_parent}"
            frames.append(FrameItem(finger_id=fid, sample_idx=idx, path=p, role="genuine"))
        for idx, p in enumerate(i_files):
            rel_parent = p.parent.relative_to(impostor_dir).as_posix()
            fid = f"impostor_{idx:04d}" if rel_parent == "." else f"impostor/{rel_parent}"
            frames.append(FrameItem(finger_id=fid, sample_idx=idx, path=p, role="impostor"))
        return frames, "genuine_impostor"

    subdirs = sorted(d for d in dataset_dir.iterdir() if d.is_dir())
    if not subdirs:
        raise ValueError(
            f"Expected <finger_id>/*.pgm or genuine/+impostor/ subdirectories in {dataset_dir}"
        )
    for d in subdirs:
        pgms = sorted(d.glob("*.pgm"))
        if not pgms:
            continue
        for idx, p in enumerate(pgms):
            frames.append(FrameItem(finger_id=d.name, sample_idx=idx, path=p, role="all"))

    fingers = {f.finger_id for f in frames}
    if len(fingers) < 2:
        raise ValueError(f"Need at least 2 finger subdirectories with .pgm files in {dataset_dir}")
    return frames, "multi_finger"


def build_pair_indices(
    frames: Sequence[FrameItem], layout: str
) -> List[Tuple[int, int, bool]]:
    """Build unordered distinct frame pairs (i < j) with genuine boolean label."""
    pairs: List[Tuple[int, int, bool]] = []
    n = len(frames)
    if layout == "genuine_impostor":
        for i in range(n):
            for j in range(i + 1, n):
                fi, fj = frames[i], frames[j]
                if fi.role == "genuine" and fj.role == "genuine":
                    if fi.finger_id == fj.finger_id:
                        pairs.append((i, j, True))
                    else:
                        pairs.append((i, j, False))
                elif fi.role != fj.role:
                    pairs.append((i, j, False))
    else:
        for i in range(n):
            for j in range(i + 1, n):
                pairs.append((i, j, frames[i].finger_id == frames[j].finger_id))
    return pairs


def resolve_or_build_brisk_cli(
    explicit_path: Optional[str], temp_root: Path
) -> Tuple[Path, Optional[Path]]:
    """Locate brisk_cli or compile it into temp_root from repo sources."""
    candidates: List[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    env_bin = os.environ.get("FTE3600_BRISK_CLI")
    if env_bin:
        candidates.append(Path(env_bin))
    script_dir = Path(__file__).resolve().parent
    candidates.extend([
        script_dir / "brisk_cli",
        Path("/usr/local/bin/brisk_cli"),
    ])
    which_bin = shutil.which("brisk_cli")
    if which_bin:
        candidates.append(Path(which_bin))

    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand, None

    repo_root = script_dir.parent.parent
    src_cli = script_dir / "brisk_cli.c"
    sam7 = repo_root / "libfprint" / "drivers" / "fte3600" / "sam7-brisk" / "libfprint"
    src_brisk = sam7 / "matchers" / "brisk" / "brisk.c"
    src_adapter = sam7 / "drivers" / "fte3600-brisk.c"
    if not (src_cli.is_file() and src_brisk.is_file() and src_adapter.is_file()):
        raise RuntimeError("brisk_cli binary not found and sam7-brisk sources missing")

    glib_cflags = subprocess.check_output(
        ["pkg-config", "--cflags", "glib-2.0"], text=True
    ).split()
    glib_libs = subprocess.check_output(
        ["pkg-config", "--libs", "glib-2.0"], text=True
    ).split()
    out_bin = temp_root / "brisk_cli"
    cmd = [
        "gcc",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-DFTE3600_ENABLE_PERSONAL_AUTH=1",
        "-fno-fast-math",
        "-ffp-contract=off",
        "-fexcess-precision=standard",
        "-frounding-math",
        f"-I{sam7 / 'drivers'}",
        f"-I{sam7 / 'matchers' / 'brisk'}",
        *glib_cflags,
        str(src_cli),
        str(src_brisk),
        str(src_adapter),
        "-o",
        str(out_bin),
        *glib_libs,
        "-lm",
    ]
    subprocess.run(cmd, check=True)
    return out_bin, out_bin


def run_brisk_matcher(
    brisk_cli: Path,
    frames: Sequence[FrameItem],
    pairs: Sequence[Tuple[int, int, bool]],
    use_clahe: bool,
) -> Dict[Tuple[int, int], PairOutcome]:
    """Evaluate all pairs via brisk_cli --batch."""
    cmd = [str(brisk_cli), "--batch", "--json"]
    if use_clahe:
        cmd.append("--clahe")

    stdin_lines = "".join(f"{frames[i].path}\t{frames[j].path}\n" for i, j, _ in pairs)
    proc = subprocess.run(
        cmd,
        input=stdin_lines,
        text=True,
        capture_output=True,
        check=True,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if len(lines) != len(pairs):
        raise RuntimeError(
            f"brisk_cli returned {len(lines)} lines for {len(pairs)} pairs"
        )

    outcomes: Dict[Tuple[int, int], PairOutcome] = {}
    for (i, j, is_gen), line in zip(pairs, lines):
        rec = json.loads(line)
        accepted = bool(rec["accepted"])
        inliers = int(rec["inliers"])
        mutual = int(rec["mutual_matches"])
        ratio = float(rec["inlier_ratio"])
        rms = float(rec["rms_error"])
        hamming = float(rec["mean_hamming"])
        match_status = int(rec.get("match_status", 0))

        if accepted:
            score = (
                100.0
                + inliers
                + 0.2 * ratio
                - 0.05 * rms
                - 0.002 * hamming
            )
        elif match_status == 0 and inliers >= 3:
            score = (
                inliers * 0.5
                + 0.1 * ratio
                + max(0.0, 1.0 - hamming / 128.0) * 0.1
            )
        else:
            score = 0.01 * mutual

        outcomes[(i, j)] = PairOutcome(
            genuine=is_gen,
            accepted=accepted,
            score=score,
            inliers=inliers,
            mutual_matches=mutual,
            inlier_ratio=ratio,
            rms_error=rms,
            mean_hamming=hamming,
            elapsed_ms=float(rec["elapsed_ms"]),
            extract_ms_a=float(rec.get("extract_a_ms", 0.0)),
            extract_ms_b=float(rec.get("extract_b_ms", 0.0)),
            features_a=int(rec["features_a"]),
            features_b=int(rec["features_b"]),
            self_ok_a=bool(rec.get("self_ok_a", False)),
            self_ok_b=bool(rec.get("self_ok_b", False)),
        )
    return outcomes


def _clahe_4x4_cv2(img: np.ndarray) -> np.ndarray:
    if int(img.max()) - int(img.min()) < 8:
        return img.copy()
    clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(4, 4))
    return clahe.apply(img)


def _spatial_variance_ok(pts: np.ndarray, min_var: float = 2.5) -> bool:
    if len(pts) < 5:
        return False
    cov = np.cov(pts.T)
    eigvals = np.linalg.eigvalsh(cov)
    return float(np.min(eigvals)) >= min_var


def run_opencv_matcher(
    algo: str,
    frames: Sequence[FrameItem],
    pairs: Sequence[Tuple[int, int, bool]],
) -> Dict[Tuple[int, int], PairOutcome]:
    """Evaluate CLAHE+AKAZE or CLAHE+SIGFM(SIFT) across all pairs."""
    if not HAS_CV2:
        raise RuntimeError("OpenCV (cv2) is not available")

    if algo == "akaze":
        detector = cv2.AKAZE_create(threshold=0.0004)
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    elif algo == "sift":
        detector = cv2.SIFT_create(nfeatures=220, contrastThreshold=0.02, edgeThreshold=15)
        matcher = cv2.BFMatcher(cv2.NORM_L2)
    else:
        raise ValueError(f"Unknown OpenCV matcher: {algo}")

    # Cache preprocessed keypoints and descriptors per frame.
    feats: List[Tuple[np.ndarray, Optional[np.ndarray], float, bool]] = []
    for f in frames:
        raw = read_pgm(f.path)
        t0 = time.perf_counter()
        proc = _clahe_4x4_cv2(raw)
        h, w = proc.shape
        up = cv2.resize(proc, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        kps, des = detector.detectAndCompute(up, None)
        t1 = time.perf_counter()
        extract_ms = (t1 - t0) * 1000.0
        if kps and des is not None and len(kps) >= 5:
            pts = np.array([[kp.pt[0] * 0.5, kp.pt[1] * 0.5] for kp in kps], dtype=np.float32)
            self_ok = len(kps) >= 8 and _spatial_variance_ok(pts, min_var=2.5)
        else:
            pts = np.zeros((0, 2), dtype=np.float32)
            des = None
            self_ok = False
        feats.append((pts, des, extract_ms, self_ok))

    outcomes: Dict[Tuple[int, int], PairOutcome] = {}
    for i, j, is_gen in pairs:
        pts_a, des_a, ex_a, ok_a = feats[i]
        pts_b, des_b, ex_b, ok_b = feats[j]
        n_fa = len(pts_a)
        n_fb = len(pts_b)

        if des_a is None or des_b is None or n_fa < 5 or n_fb < 5:
            outcomes[(i, j)] = PairOutcome(
                genuine=is_gen,
                accepted=False,
                score=0.0,
                inliers=0,
                mutual_matches=0,
                inlier_ratio=0.0,
                rms_error=0.0,
                mean_hamming=0.0,
                elapsed_ms=0.0,
                extract_ms_a=ex_a,
                extract_ms_b=ex_b,
                features_a=n_fa,
                features_b=n_fb,
                self_ok_a=ok_a,
                self_ok_b=ok_b,
            )
            continue

        t0 = time.perf_counter()
        if algo == "akaze":
            good = matcher.match(des_a, des_b)
        else:
            knn = matcher.knnMatch(des_a, des_b, k=2)
            good = [m_n[0] for m_n in knn if len(m_n) == 2 and m_n[0].distance < 0.78 * m_n[1].distance]

        n_mutual = len(good)
        inliers = 0
        rms = 0.0
        mean_dist = 0.0
        inlier_ratio = 0.0
        accepted = False
        score = 0.01 * n_mutual

        if n_mutual >= 5:
            src_pts = np.array([pts_a[m.queryIdx] for m in good], dtype=np.float32).reshape(-1, 1, 2)
            dst_pts = np.array([pts_b[m.trainIdx] for m in good], dtype=np.float32).reshape(-1, 1, 2)
            mat, mask = cv2.estimateAffinePartial2D(
                src_pts,
                dst_pts,
                method=cv2.RANSAC,
                ransacReprojThreshold=2.0,
                maxIters=2000,
                confidence=0.99,
            )
            if mat is not None and mask is not None:
                scale = math.hypot(float(mat[0, 0]), float(mat[1, 0]))
                if 0.85 <= scale <= 1.15:
                    inlier_mask = mask.ravel().astype(bool)
                    inliers = int(np.sum(inlier_mask))
                    if inliers >= 4:
                        inlier_src = src_pts[inlier_mask, 0, :]
                        inlier_dst = dst_pts[inlier_mask, 0, :]
                        pred = (inlier_src @ mat[:, :2].T) + mat[:, 2]
                        errs = np.linalg.norm(pred - inlier_dst, axis=1)
                        rms = float(np.sqrt(np.mean(errs ** 2)))
                        inlier_ratio = inliers / float(n_mutual)
                        dists = [good[k].distance for k in range(n_mutual) if inlier_mask[k]]
                        mean_dist = float(np.mean(dists)) if dists else 0.0
                        spread_ok = _spatial_variance_ok(inlier_src, min_var=2.5) and _spatial_variance_ok(inlier_dst, min_var=2.5)
                        accepted = (
                            inliers >= 5
                            and inlier_ratio >= 0.20
                            and rms < 1.8
                            and spread_ok
                        )
                        if accepted:
                            score = 100.0 + inliers + 0.2 * inlier_ratio - 0.05 * rms
                        else:
                            score = inliers * 0.5 + 0.1 * inlier_ratio

        t1 = time.perf_counter()
        match_ms = (t1 - t0) * 1000.0

        outcomes[(i, j)] = PairOutcome(
            genuine=is_gen,
            accepted=accepted,
            score=score,
            inliers=inliers,
            mutual_matches=n_mutual,
            inlier_ratio=inlier_ratio,
            rms_error=rms,
            mean_hamming=mean_dist,
            elapsed_ms=match_ms,
            extract_ms_a=ex_a,
            extract_ms_b=ex_b,
            features_a=n_fa,
            features_b=n_fb,
            self_ok_a=ok_a,
            self_ok_b=ok_b,
        )
    return outcomes


def compute_roc_metrics(outcomes: Sequence[PairOutcome]) -> Dict[str, float]:
    gen_scores = np.array([o.score for o in outcomes if o.genuine], dtype=np.float64)
    imp_scores = np.array([o.score for o in outcomes if not o.genuine], dtype=np.float64)
    if len(gen_scores) == 0 or len(imp_scores) == 0:
        raise ValueError("Need both genuine and impostor pairs to compute ROC metrics")

    thresholds = np.unique(np.concatenate([gen_scores, imp_scores]))
    thresholds = np.append(thresholds, np.max(thresholds) + 1.0)
    thresholds.sort()

    tars = np.array([np.mean(gen_scores >= th) for th in thresholds], dtype=np.float64)
    fars = np.array([np.mean(imp_scores >= th) for th in thresholds], dtype=np.float64)
    frrs = 1.0 - tars

    # TAR @ FAR=0%
    zero_mask = fars == 0.0
    tar_far0 = float(np.max(tars[zero_mask])) if np.any(zero_mask) else 0.0

    # TAR @ FAR<=1%
    one_pct_mask = fars <= 0.01
    tar_far1 = float(np.max(tars[one_pct_mask])) if np.any(one_pct_mask) else 0.0

    # EER via linear interpolation around crossing of FAR and FRR
    diff = fars - frrs
    eer = 1.0
    best_idx = int(np.argmin(np.abs(diff)))
    if diff[best_idx] == 0.0:
        eer = float(fars[best_idx])
    else:
        # Find crossing interval where fars >= frrs transitions to fars <= frrs
        eer = float(0.5 * (fars[best_idx] + frrs[best_idx]))
        for k in range(len(thresholds) - 1):
            if (diff[k] >= 0.0 and diff[k + 1] <= 0.0) or (diff[k] <= 0.0 and diff[k + 1] >= 0.0):
                d0, d1 = diff[k], diff[k + 1]
                if d1 != d0:
                    w = -d0 / (d1 - d0)
                    eer = float(fars[k] + w * (fars[k + 1] - fars[k]))
                else:
                    eer = float(0.5 * (fars[k] + frrs[k]))
                break

    gate_tar = float(np.mean([1.0 if o.accepted else 0.0 for o in outcomes if o.genuine]))
    gate_far = float(np.mean([1.0 if o.accepted else 0.0 for o in outcomes if not o.genuine]))
    mean_match_ms = float(np.mean([o.elapsed_ms for o in outcomes]))

    # Unique per-frame extraction time estimate (average of observed extract_ms_a/b)
    mean_extract_ms = float(
        np.mean([0.5 * (o.extract_ms_a + o.extract_ms_b) for o in outcomes])
    )

    return {
        "tar_far0": tar_far0 * 100.0,
        "tar_far1": tar_far1 * 100.0,
        "eer": eer * 100.0,
        "gate_tar": gate_tar * 100.0,
        "gate_far": gate_far * 100.0,
        "mean_match_ms": mean_match_ms,
        "mean_extract_ms": mean_extract_ms,
        "n_genuine": int(len(gen_scores)),
        "n_impostor": int(len(imp_scores)),
    }


def _pair_lookup(
    outcomes: Dict[Tuple[int, int], PairOutcome], a: int, b: int
) -> PairOutcome:
    return outcomes[(a, b)] if a < b else outcomes[(b, a)]


def evaluate_gallery_verification(
    frames: Sequence[FrameItem],
    layout: str,
    outcomes: Dict[Tuple[int, int], PairOutcome],
) -> Dict[str, float]:
    """Evaluate 8-sample vs 12-sample enrolled template gallery TAR @ FAR=0%."""
    if layout == "genuine_impostor":
        gen_idxs = [idx for idx, f in enumerate(frames) if f.role == "genuine"]
        imp_idxs = [idx for idx, f in enumerate(frames) if f.role == "impostor"]
        if len(gen_idxs) < 4:
            return {"g8_tar_far0": float("nan"), "g12_tar_far0": float("nan")}
        # Use up to 12 gallery samples and remaining (or LOO if <=12)
        results: Dict[str, float] = {}
        for k in (8, 12):
            gal = gen_idxs[: min(k, len(gen_idxs) - 1)]
            probes_g = gen_idxs[len(gal):]
            gen_scores = [
                max(_pair_lookup(outcomes, p, g).score for g in gal) for p in probes_g
            ]
            imp_scores = [
                max(_pair_lookup(outcomes, p, g).score for g in gal) for p in imp_idxs
            ]
            if gen_scores and imp_scores:
                max_imp = max(imp_scores)
                tar = sum(1 for s in gen_scores if s > max_imp) / float(len(gen_scores))
                results[f"g{k}_tar_far0"] = tar * 100.0
                results[f"g{k}_gate_tar"] = (
                    sum(1 for p in probes_g if any(_pair_lookup(outcomes, p, g).accepted for g in gal))
                    / float(len(probes_g))
                    * 100.0
                )
            else:
                results[f"g{k}_tar_far0"] = float("nan")
                results[f"g{k}_gate_tar"] = float("nan")
        return results

    by_finger: Dict[str, List[int]] = {}
    for idx, f in enumerate(frames):
        by_finger.setdefault(f.finger_id, []).append(idx)

    fingers = sorted(by_finger.keys())
    max_n = min(len(v) for v in by_finger.values())
    if max_n < 4:
        return {
            "g8_tar_far0": float("nan"),
            "g12_tar_far0": float("nan"),
            "g8_gate_tar": float("nan"),
            "g12_gate_tar": float("nan"),
            "g8_gate_far": float("nan"),
            "g12_gate_far": float("nan"),
        }

    # If >=14 samples per finger, reserve [12:] (4 probes) as common held-out probe set
    # for both 8-sample [0:8] and 12-sample [0:12] galleries. Otherwise use LOO/split.
    results = {}
    for k in (8, 12):
        gen_scores: List[float] = []
        imp_scores: List[float] = []
        gen_gate_hits = 0
        imp_gate_hits = 0

        for f_id in fingers:
            idxs = by_finger[f_id]
            if len(idxs) >= 14:
                gal = idxs[:k]
                probes = idxs[12:]
            elif len(idxs) > k:
                gal = idxs[:k]
                probes = idxs[k:]
            else:
                # Leave-one-out fallback when dataset has <= k captures per finger
                gal = idxs[: len(idxs) - 1]
                probes = [idxs[-1]]

            other_probes = [
                p
                for of_id in fingers
                if of_id != f_id
                for p in (by_finger[of_id][12:] if len(by_finger[of_id]) >= 14 else by_finger[of_id][-min(4, len(by_finger[of_id])):])
            ]

            for p in probes:
                pair_list = [_pair_lookup(outcomes, p, g) for g in gal]
                gen_scores.append(max(o.score for o in pair_list))
                if any(o.accepted for o in pair_list):
                    gen_gate_hits += 1

            for p in other_probes:
                pair_list = [_pair_lookup(outcomes, p, g) for g in gal]
                imp_scores.append(max(o.score for o in pair_list))
                if any(o.accepted for o in pair_list):
                    imp_gate_hits += 1

        max_imp = max(imp_scores) if imp_scores else 0.0
        # Also evaluate TAR @ FAR=0% (strictly above all impostor gallery max scores)
        tar_far0 = (
            sum(1 for s in gen_scores if s > max_imp) / float(len(gen_scores)) * 100.0
            if gen_scores
            else float("nan")
        )
        results[f"g{k}_tar_far0"] = tar_far0
        results[f"g{k}_gate_tar"] = (
            gen_gate_hits / float(len(gen_scores)) * 100.0 if gen_scores else float("nan")
        )
        results[f"g{k}_gate_far"] = (
            imp_gate_hits / float(len(imp_scores)) * 100.0 if imp_scores else float("nan")
        )
    return results


def _self_ok_for_idx(outcomes: Dict[Tuple[int, int], PairOutcome], idx: int, other: int) -> bool:
    if idx < other:
        return outcomes[(idx, other)].self_ok_a
    return outcomes[(other, idx)].self_ok_b


def _tier2_consistent(o: PairOutcome, is_brisk: bool) -> bool:
    if o.accepted:
        return True
    if is_brisk:
        return (
            (o.inliers >= 4 and o.rms_error < 1.8 and o.mean_hamming <= 64.0)
            or (o.mutual_matches >= 6 and o.inliers >= 3 and o.mean_hamming <= 62.0)
        )
    return o.inliers >= 4 and o.rms_error < 2.2


def simulate_enrollment(
    frames: Sequence[FrameItem],
    layout: str,
    outcomes: Dict[Tuple[int, int], PairOutcome],
    is_brisk: bool,
    target_samples: int = 8,
) -> Dict[str, float]:
    """Simulate enrollment stage acceptance rate: strict gate vs two-tier gate."""
    by_finger: Dict[str, List[int]] = {}
    for idx, f in enumerate(frames):
        if layout == "genuine_impostor" and f.role != "genuine":
            continue
        by_finger.setdefault(f.finger_id, []).append(idx)

    fingers = sorted(by_finger.keys())
    if not fingers:
        return {}

    res: Dict[str, float] = {}
    for mode in ("strict", "two_tier"):
        total_attempts = 0
        total_accepted = 0
        completed_fingers = 0

        for f_id in fingers:
            idxs = by_finger[f_id]
            if len(idxs) < 2:
                continue
            other_ref = idxs[1]
            enrolled: List[int] = []
            for pos, idx in enumerate(idxs):
                if len(enrolled) >= target_samples:
                    break
                total_attempts += 1
                ref_idx = idxs[0] if pos > 0 else other_ref
                if not _self_ok_for_idx(outcomes, idx, ref_idx):
                    continue
                if not enrolled:
                    enrolled.append(idx)
                    total_accepted += 1
                    continue

                if mode == "strict":
                    ok = any(_pair_lookup(outcomes, idx, e).accepted for e in enrolled)
                else:
                    ok = any(_tier2_consistent(_pair_lookup(outcomes, idx, e), is_brisk) for e in enrolled)
                if ok:
                    enrolled.append(idx)
                    total_accepted += 1

            if len(enrolled) >= min(target_samples, len(idxs)):
                completed_fingers += 1

        res[f"{mode}_accept_rate"] = (
            total_accepted / float(total_attempts) * 100.0 if total_attempts else 0.0
        )
        res[f"{mode}_completion_rate"] = (
            completed_fingers / float(len(fingers)) * 100.0 if fingers else 0.0
        )
    return res


def format_markdown_report(
    results: Sequence[Tuple[str, Dict[str, float], Dict[str, float], Dict[str, float]]],
    dataset_desc: str,
) -> str:
    lines: List[str] = []
    lines.append(f"### Dataset: {dataset_desc}")
    lines.append("")
    lines.append("#### 1. Pairwise Verification ROC & Latency")
    lines.append("")
    lines.append(
        "| Matcher | TAR @ FAR=0% | TAR @ FAR<=1% | EER | Gate TAR / FAR | Mean Match (ms) | Mean Extract (ms) |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for name, roc, _, _ in results:
        lines.append(
            f"| `{name}` | {roc['tar_far0']:.2f}% | {roc['tar_far1']:.2f}% | "
            f"{roc['eer']:.2f}% | {roc['gate_tar']:.2f}% / {roc['gate_far']:.2f}% | "
            f"{roc['mean_match_ms']:.3f} | {roc['mean_extract_ms']:.3f} |"
        )

    lines.append("")
    lines.append("#### 2. Multi-Sample Template Gallery (8-Sample vs 12-Sample, TAR @ FAR=0%)")
    lines.append("")
    lines.append(
        "| Matcher | 8-Sample TAR @ FAR=0% | 12-Sample TAR @ FAR=0% | 8-Sample Gate TAR / FAR | 12-Sample Gate TAR / FAR |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for name, _, gal, _ in results:
        g8 = f"{gal['g8_tar_far0']:.2f}%" if not math.isnan(gal["g8_tar_far0"]) else "n/a"
        g12 = f"{gal['g12_tar_far0']:.2f}%" if not math.isnan(gal["g12_tar_far0"]) else "n/a"
        gt8 = (
            f"{gal['g8_gate_tar']:.2f}% / {gal['g8_gate_far']:.2f}%"
            if not math.isnan(gal.get("g8_gate_tar", float("nan")))
            else "n/a"
        )
        gt12 = (
            f"{gal['g12_gate_tar']:.2f}% / {gal['g12_gate_far']:.2f}%"
            if not math.isnan(gal.get("g12_gate_tar", float("nan")))
            else "n/a"
        )
        lines.append(f"| `{name}` | {g8} | {g12} | {gt8} | {gt12} |")

    lines.append("")
    lines.append("#### 3. Simulated Enrollment Stage Acceptance (8-Sample Target)")
    lines.append("")
    lines.append(
        "| Matcher | Strict Touch Accept | Strict Enroll Complete | Two-Tier Touch Accept | Two-Tier Enroll Complete |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for name, _, _, enr in results:
        lines.append(
            f"| `{name}` | {enr.get('strict_accept_rate', 0.0):.2f}% | "
            f"{enr.get('strict_completion_rate', 0.0):.2f}% | "
            f"{enr.get('two_tier_accept_rate', 0.0):.2f}% | "
            f"{enr.get('two_tier_completion_rate', 0.0):.2f}% |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="FTE3600 64x80 offline multi-algorithm matcher benchmark"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--synthetic",
        action="store_true",
        default=True,
        help="Run deterministic synthetic ridge stress benchmark (default)",
    )
    mode.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="Evaluate real FTE3600_DUMP_DIR 64x80 .pgm captures (<finger_id>/*.pgm or genuine/+impostor/)",
    )
    parser.add_argument(
        "--fingers", type=int, default=10, help="Synthetic finger count (default: 10)"
    )
    parser.add_argument(
        "--captures",
        type=int,
        default=16,
        help="Synthetic captures per finger (default: 16 = 12 gallery + 4 held-out probes)",
    )
    parser.add_argument(
        "--seed", type=int, default=36002026, help="PRNG seed for synthetic mode"
    )
    parser.add_argument(
        "--brisk-only",
        action="store_true",
        help="Run only BRISK-baseline and CLAHE+BRISK (skip OpenCV AKAZE/SIFT)",
    )
    parser.add_argument(
        "--brisk-cli",
        type=str,
        default=None,
        help="Explicit path to compiled brisk_cli binary",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional output path for full JSON metrics report",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="fte3600-bench-") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        brisk_cli, _ = resolve_or_build_brisk_cli(args.brisk_cli, tmp_dir)

        if args.dataset is not None:
            frames, layout = load_dataset(args.dataset)
            n_fingers = len({f.finger_id for f in frames})
            dataset_desc = (
                f"Real capture dump `{args.dataset}` ({len(frames)} frames, "
                f"{n_fingers} identities, layout={layout})"
            )
        else:
            synth_dir = tmp_dir / "synthetic"
            frames = generate_synthetic_dataset(
                synth_dir,
                n_fingers=args.fingers,
                n_captures=args.captures,
                seed=args.seed,
            )
            layout = "multi_finger"
            dataset_desc = (
                f"Deterministic synthetic ridge stress suite "
                f"({args.fingers} fingers x {args.captures} captures = {len(frames)} frames, "
                f"seed={args.seed})"
            )

        pairs = build_pair_indices(frames, layout)
        n_gen = sum(1 for _, _, g in pairs if g)
        n_imp = len(pairs) - n_gen
        print(
            f"Evaluating {len(frames)} frames ({n_gen} genuine pairs, {n_imp} impostor pairs)...",
            file=sys.stderr,
        )

        matchers: List[Tuple[str, str]] = [
            ("BRISK-baseline", "brisk_base"),
            ("CLAHE+BRISK", "brisk_clahe"),
        ]
        if args.brisk_only:
            pass
        elif not HAS_CV2:
            print(
                "Note: Python OpenCV (cv2) not installed; falling back to BRISK-only mode.",
                file=sys.stderr,
            )
        else:
            matchers.extend([
                ("CLAHE+AKAZE", "akaze"),
                ("CLAHE+SIGFM(SIFT)", "sift"),
            ])

        results = []
        json_payload: Dict[str, object] = {
            "dataset": dataset_desc,
            "frames": len(frames),
            "genuine_pairs": n_gen,
            "impostor_pairs": n_imp,
            "opencv_available": HAS_CV2,
            "matchers": {},
        }

        for display_name, key in matchers:
            if key == "brisk_base":
                outcomes = run_brisk_matcher(brisk_cli, frames, pairs, use_clahe=False)
                is_brisk = True
            elif key == "brisk_clahe":
                outcomes = run_brisk_matcher(brisk_cli, frames, pairs, use_clahe=True)
                is_brisk = True
            else:
                outcomes = run_opencv_matcher(key, frames, pairs)
                is_brisk = False

            roc = compute_roc_metrics(list(outcomes.values()))
            gal = evaluate_gallery_verification(frames, layout, outcomes)
            enr = simulate_enrollment(frames, layout, outcomes, is_brisk=is_brisk)
            results.append((display_name, roc, gal, enr))
            assert isinstance(json_payload["matchers"], dict)
            json_payload["matchers"][display_name] = {
                "roc": roc,
                "gallery": gal,
                "enrollment": enr,
            }

        md = format_markdown_report(results, dataset_desc)
        print(md)

        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(json_payload, indent=2) + "\n")
            print(f"Wrote JSON report to {args.json_out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
