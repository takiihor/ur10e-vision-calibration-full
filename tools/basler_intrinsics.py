#!/usr/bin/env python3
"""Fit lens intrinsics from original pylon PNG/TIFF/BMP images, without hardware access."""
import argparse
import json
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "src/anodizing-modules/vendor"
for package in ("charuco-generator", "charuco-realsense"):
    sys.path.insert(0, str(VENDOR / package / "src"))

from charuco_generator.board import reconstruct_board
from charuco_generator.validation import load_manifest
from charuco_realsense.calibration import opencv_calibration
from charuco_realsense.detect import CharucoDetector


def observations(folder, board, size):
    folder = Path(folder)
    files = sorted(p for p in folder.iterdir()
                   if p.suffix.lower() in {".png", ".tif", ".tiff", ".bmp"})
    if not files:
        raise ValueError(f"No PNG/TIFF/BMP images in {folder}")
    detector = CharucoDetector(board)
    points = board.getChessboardCorners()
    views = []
    for path in files:
        gray = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if gray is None or gray.dtype != np.uint8:
            raise ValueError(f"{path}: use original 8-bit Mono8 images")
        if gray.ndim == 3:
            gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        if (gray.shape[1], gray.shape[0]) != size:
            raise ValueError(f"{path}: size differs from {size}; do not resize calibration images")
        found = detector.detect(gray)
        if len(found.charuco_ids) < 12:
            print(f"Skipping {path.name}: {len(found.charuco_ids)} corners (need 12)")
            continue
        ids = np.array(found.charuco_ids, dtype=np.int32)
        if board.checkCharucoCornersCollinear(ids):
            print(f"Skipping {path.name}: corners are collinear")
            continue
        views.append((path, points[ids].astype(np.float32), found.charuco_corners))
        print(f"Using {path.name}: {len(ids)}/{len(points)} corners")
    return views


def reprojection_error(objects, image_points, matrix, distortion, rotation, translation):
    projected, _ = cv2.projectPoints(objects, rotation, translation, matrix, distortion)
    return float(np.sqrt(np.mean(np.sum((projected - image_points) ** 2, axis=2))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", required=True, help="Folder of training images")
    parser.add_argument("--check", help="Separate folder of fresh images, excluded from fitting")
    parser.add_argument("--serial", required=True, help="Actual camera serial number")
    parser.add_argument("--output", required=True, help="New intrinsics JSON; never overwrites")
    parser.add_argument("--manifest", type=Path, default=ROOT / "src/anodizing-modules/data/boards/calibration_stage2/board_manifest.json")
    parser.add_argument("--board", default="calibration_stage2")
    parser.add_argument("--width", type=int, default=2448)
    parser.add_argument("--height", type=int, default=2048)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise ValueError(f"{output} exists; choose a new output filename")
    if not args.serial.strip():
        raise ValueError("Camera serial must not be empty")
    size = (args.width, args.height)
    if min(size) <= 0:
        raise ValueError("Image dimensions must be positive")
    manifest = load_manifest(str(args.manifest))
    board = reconstruct_board(manifest, args.board)
    views = observations(args.images, board, size)
    if len(views) < 12:
        raise ValueError(f"Only {len(views)} usable training views; capture at least 12, preferably 25–30")
    checks = observations(args.check, board, size) if args.check else []
    if args.check and len(checks) < 5:
        raise ValueError("Capture at least five usable fresh check images")
    train_paths = {path.resolve() for path, _, _ in views}
    if any(path.resolve() in train_paths for path, _, _ in checks):
        raise ValueError("Training and check images must be separate")
    rmse, matrix, distortion, rotations, _, _, _, per_view = cv2.calibrateCameraExtended(
        [v[1] for v in views], [v[2] for v in views], size, None, None,
        criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 100, 1e-9))
    result = {
        "camera_provider": "basler", "camera_serial": args.serial.strip(),
        "width": args.width, "height": args.height,
        "fx": float(matrix[0, 0]), "fy": float(matrix[1, 1]),
        "ppx": float(matrix[0, 2]), "ppy": float(matrix[1, 2]),
        "model": "opencv", "coeffs": distortion.ravel().tolist(),
    }
    opencv_calibration(result, size)
    if not (np.isfinite(rmse) and 0 <= result["ppx"] < args.width
            and 0 <= result["ppy"] < args.height):
        raise ValueError("Invalid fit; capture sharper views with varied tilts and positions")
    normals = np.array([cv2.Rodrigues(r)[0][:, 2] for r in rotations])
    tilt_span = float(np.degrees(np.arccos(np.clip(normals @ normals.T, -1, 1))).max())
    pixels = np.concatenate([v[2].reshape(-1, 2) for v in views])
    coverage = float(cv2.contourArea(cv2.convexHull(pixels)) / (args.width * args.height))
    warnings = []
    if tilt_span < 20:
        warnings.append("Limited tilt diversity: tip the board forward/backward AND left/right")
    if coverage < 0.4:
        warnings.append("Limited coverage: capture corners near all image edges and corners")
    if rmse > 0.5:
        warnings.append("Training RMS exceeds 0.5 px; inspect blur, print flatness and corner quality")
    check_errors = []
    for path, objects, image_points in checks:
        ok, rotation, translation = cv2.solvePnP(objects, image_points, matrix, distortion)
        if not ok:
            raise ValueError(f"Could not estimate check-board pose for {path}")
        error = reprojection_error(objects, image_points, matrix, distortion, rotation, translation)
        check_errors.append({"image": str(path.resolve()), "reprojection_rmse_px": error})
        print(f"Fresh check {path.name}: {error:.3f} px RMS")
    if any(c["reprojection_rmse_px"] > 0.5 for c in check_errors):
        warnings.append("Some fresh checks exceed 0.5 px; review before using the intrinsics")
    result["estimation"] = {
        "validated": False, "method": "opencv_charuco_standard_5_coefficients",
        "manifest": str(args.manifest.resolve()), "board": args.board,
        "reprojection_rmse_px": float(rmse), "image_coverage_fraction": coverage,
        "board_normal_span_deg": tilt_span,
        "per_view": [{"image": str(v[0].resolve()), "reprojection_rmse_px": float(e)}
                     for v, e in zip(views, per_view.ravel())],
        "fresh_checks": check_errors, "warnings": warnings,
        "note": "Image reprojection checks do not establish physical measurement accuracy. "
                "Review board dimensions, coverage, sharpness and independent measurements.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(f"Training RMS: {rmse:.3f} px; image coverage: {coverage:.0%}; tilt span: {tilt_span:.1f} deg")
    for warning in warnings:
        print(f"Review: {warning}")
    print(f"Wrote {output.resolve()} (estimate; requires review)")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, cv2.error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
