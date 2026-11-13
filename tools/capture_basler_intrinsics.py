#!/usr/bin/env python3
"""Guide a hands-free Basler ChArUco capture and fit the resulting intrinsics."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic
from types import SimpleNamespace

from basler_intrinsics import (
    ROOT, CharucoDetector, load_manifest, observations, opencv_calibration,
    reconstruct_board, reprojection_error,
)
from charuco_realsense.basler import BaslerCamera
import cv2
import numpy as np


SIZE = (2448, 2048)
WINDOW = "Basler intrinsics capture"
BOARD = "calibration_stage2"
DATA = ROOT / ".ui-data" / "basler-intrinsics"
MANIFEST = ROOT / "src/anodizing-modules/data/boards/calibration_stage2/board_manifest.json"
TRAIN_POSES = (
    "Centre, facing the camera",
    "Centre, left edge closer (20-40 degrees)",
    "Centre, right edge closer (20-40 degrees)",
    "Centre, top edge closer (20-40 degrees)",
    "Centre, bottom edge closer (20-40 degrees)",
    "Near the top of the image, slight left tilt",
    "Near the bottom of the image, slight right tilt",
    "Near the left of the image, slight top tilt",
    "Near the right of the image, slight bottom tilt",
    "Upper left, facing the camera",
    "Upper right, slight bottom tilt",
    "Lower left, slight right tilt",
    "Lower right, slight left tilt",
    "Slightly closer, centre, facing the camera",
    "Slightly closer, left edge closer",
    "Slightly closer, right edge closer",
    "Slightly closer, top edge closer",
    "Slightly closer, bottom edge closer",
    "Slightly farther, centre, facing the camera",
    "Slightly farther, upper left, right edge closer",
    "Slightly farther, upper right, top edge closer",
    "Slightly farther, lower left, bottom edge closer",
    "Slightly farther, lower right, left edge closer",
    "Upper half, tilt on both axes",
    "Lower half, tilt on both axes in the other direction",
    "Left half, new compound tilt",
    "Right half, new compound tilt",
    "Upper left of the image, slight right tilt",
    "Upper right of the image, slight bottom tilt",
    "Lower left of the image, slight top tilt",
)
CHECK_POSES = (
    "Fresh view: centre-left, top-right corner closer",
    "Fresh view: centre-right, bottom-left corner closer",
    "Fresh view: upper right, slightly closer, left edge closer",
    "Fresh view: lower left, slightly farther, right edge closer",
    "Fresh view: upper left, bottom edge closer",
    "Fresh view: lower right, top edge closer",
    "Fresh view: centre, new distance and compound tilt",
    "Fresh view: left edge, new distance and right tilt",
    "Fresh view: right edge, new distance and left tilt",
    "Fresh view: top edge, new distance and bottom tilt",
)
EDGE_TARGETS = {
    "train": {28: ("left", "top"), 29: ("right", "top"), 30: ("left", "bottom")},
    "check": {8: ("left",), 9: ("right",), 10: ("top",)},
}
VERIFY_POSES = (
    "Centre, facing the camera",
    "Centre, left edge closer (20-40 degrees)",
    "Centre, right edge closer (20-40 degrees)",
    "Near the left edge, top edge closer (20-40 degrees)",
    "Near the right edge, bottom edge closer (20-40 degrees)",
    "Near the top edge, right edge closer (20-40 degrees)",
    "Near the bottom edge, left edge closer (20-40 degrees)",
)
VERIFY_EDGES = {4: ("left",), 5: ("right",), 6: ("top",), 7: ("bottom",)}


def distance(first, second):
    common = sorted(first.keys() & second.keys())
    if len(common) < 12:
        return float("inf")
    offsets = np.array([first[i] - second[i] for i in common])
    return float(np.sqrt(np.mean(np.sum(offsets**2, axis=1))))


def inspect_frame(detector, board, gray, target_edges=()):
    found = detector.detect(gray)
    corners = {i: xy.ravel() for i, xy in zip(found.charuco_ids, found.charuco_corners)}
    total = len(board.getChessboardCorners())
    if len(corners) < max(12, int(np.ceil(total * 0.8))):
        return corners, f"Corners {len(corners)}/{total}: show the full board and check focus."
    square = board.getSquareLength()
    width, height = board.getChessboardSize()
    boundary = np.float32([[[0, 0], [width * square, 0],
                            [width * square, height * square], [0, height * square]]])
    object_xy = board.getChessboardCorners()[list(corners), :2].copy()
    homography, _ = cv2.findHomography(object_xy, np.array(list(corners.values())))
    if homography is None:
        return corners, "Show the whole board at a gentler angle."
    outer = cv2.perspectiveTransform(boundary, homography)[0]
    if not (np.isfinite(outer).all() and np.all(outer >= 5) and np.all(outer <= np.array(SIZE) - 6)):
        return corners, "Keep the whole pattern inside the frame."
    if target_edges:
        x, y = (outer / SIZE).T
        gaps = {"left": x.min(), "right": 1 - x.max(), "top": y.min(), "bottom": 1 - y.max()}
        remaining = [edge for edge in target_edges if gaps[edge] > 0.12]
        if remaining:
            return corners, f"Move the pattern closer to the {' and '.join(remaining)} image edge."
    return corners, ""


def brightness(gray):
    low, median, high = np.percentile(gray[::4, ::4], (1, 50, 99)).astype(int)
    return low, median, high


def camera_config(args):
    return SimpleNamespace(serial=args.serial, width=SIZE[0], height=SIZE[1], fps=args.fps,
                           exposure_time_us=args.exposure_us, gain_db=args.gain_db)


def read_gray(camera):
    return np.ascontiguousarray(camera.read().bgr[:, :, 0])


def check_pose(board, objects, pixels, calibration):
    matrix, distortion = calibration
    ok, rotation, translation = cv2.solvePnP(objects, pixels, matrix, distortion)
    if not ok:
        raise ValueError("Could not estimate the check-board pose.")
    orientation, _ = cv2.Rodrigues(rotation)
    width, height = board.getChessboardSize()
    centre = orientation @ (np.array([width, height, 0]) * board.getSquareLength() / 2) + translation.ravel()
    angle = float(np.degrees(np.arccos(np.clip(orientation[2, 2], -1, 1))))
    return rotation, translation, angle, float(np.linalg.norm(centre) * 1000)


def review_verification(folder, board, calibration, source):
    checks = []
    for path, objects, pixels in observations(folder, board, SIZE):
        rotation, translation, angle, distance_mm = check_pose(board, objects, pixels, calibration)
        rms = reprojection_error(objects, pixels, *calibration, rotation, translation)
        checks.append({"image": str(path), "reprojection_rmse_px": rms,
                       "tilt_deg": angle, "distance_mm": distance_mm})
        print(f"{path.name}: {rms:.3f} px RMS, {angle:.1f} deg, {distance_mm:.0f} mm", flush=True)
    if len(checks) != len(VERIFY_POSES):
        raise ValueError(f"Expected {len(VERIFY_POSES)} usable verification photos, found {len(checks)}.")
    passed = all(check["reprojection_rmse_px"] < 0.5 for check in checks)
    report = {"intrinsics": str(source.resolve()), "checks": checks, "threshold_px": 0.5,
              "passed": passed, "working_distance_mm": [300, 400], "tilted_views_deg": [20, 40]}
    path = folder / "verification.json"
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"Working-range reprojection check: {'PASS' if passed else 'FAIL'}. Report: {path}")
    return 0 if passed else 1


def show_preview(gray, corners, title, pose, status, level=""):
    preview = cv2.resize(gray, (765, 640), interpolation=cv2.INTER_AREA)
    preview = cv2.cvtColor(preview, cv2.COLOR_GRAY2BGR)
    for point in corners.values():
        cv2.circle(preview, tuple(np.rint(point * (765 / SIZE[0])).astype(int)), 3, (80, 225, 100), -1)
    panel = np.full((128, 765, 3), 25, dtype=np.uint8)
    for y, message, scale in ((26, title, 0.64), (54, pose, 0.58), (84, status, 0.55),
                              (111, level, 0.48)):
        cv2.putText(panel, message, (14, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (245, 245, 245), 1, cv2.LINE_AA)
    cv2.imshow(WINDOW, np.vstack((panel, preview)))
    return cv2.waitKey(1) & 0xFF


def existing_poses(folder, board):
    if not any(p.suffix.lower() in {".png", ".tif", ".tiff", ".bmp"} for p in folder.iterdir()):
        return []
    indices = {tuple(xy): i for i, xy in enumerate(board.getChessboardCorners())}
    return [{indices[tuple(xyz)]: xy.ravel() for xyz, xy in zip(objects, pixels)}
            for _, objects, pixels in observations(folder, board, SIZE)]


def check_gige_access():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM):
            pass
    except PermissionError as exc:
        raise RuntimeError("GigE access is blocked here. Run this command in the camera PC's desktop Terminal.") from exc


def diagnose(args):
    check_gige_access()
    from pypylon import genicam

    try:
        with BaslerCamera(camera_config(args)) as camera:
            gray = read_gray(camera)
            metadata = camera.metadata
    except genicam.GenericException as exc:
        raise RuntimeError(f"Basler {args.serial}: {exc}") from exc
    low, median, high = brightness(gray)
    print(f"Basler {args.serial}: {SIZE[0]} x {SIZE[1]} Mono8, "
          f"exposure {metadata['exposure_time_us']:g} us, gain {metadata['gain_db']:g} dB.")
    print(f"Gray levels (0-255): min={gray.min()}, p01={low}, median={median}, p99={high}, max={gray.max()}.")
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "preview-diagnostic.png"
    if not cv2.imwrite(str(path), gray):
        raise RuntimeError(f"Could not save diagnostic frame to {path}.")
    print(f"Saved the original frame to {path}.")
    if high <= 20:
        print("The camera frame is very dark. Check the lens cap and iris, light the board, or increase exposure.")
    return 0


def capture(args, board, verification=None, verification_folder=None):
    detector = CharucoDetector(board)
    sets = [(DATA / "train", TRAIN_POSES[:args.train_count], EDGE_TARGETS["train"]),
            (DATA / "check", CHECK_POSES[:args.check_count], EDGE_TARGETS["check"])]
    if verification is not None:
        sets = [(verification_folder, VERIFY_POSES, VERIFY_EDGES)]
    for folder, _, _ in sets:
        folder.mkdir(parents=True, exist_ok=True)
    saved = [existing_poses(folder, board) for folder, _, _ in sets]
    if all(len(views) >= len(poses) for views, (_, poses, _) in zip(saved, sets)):
        return True

    check_gige_access()
    print(f"Opening Basler {args.serial}. Close pylon Viewer if it is holding the camera.", flush=True)
    from pypylon import genicam

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW, 765, 768)
    try:
        show_preview(np.zeros(SIZE[::-1], dtype=np.uint8), {}, f"Basler {args.serial}",
                     "Waiting for the first camera frame", "Check the Terminal if the camera does not respond.")
        with BaslerCamera(camera_config(args)) as camera:
            profile_dir = DATA if verification is None else sets[0][0]
            profile_path = profile_dir / "capture-profile.json"
            profile = dict(camera.metadata, board=args.board, manifest=str(args.manifest.resolve()))
            if profile_path.exists() and json.loads(profile_path.read_text()) != profile:
                if any(saved):
                    raise ValueError("Camera or board settings differ from the previous capture session.")
                profile_path.write_text(json.dumps(profile, indent=2) + "\n")
            elif not profile_path.exists():
                profile_path.write_text(json.dumps(profile, indent=2) + "\n")
            print(f"Opened {camera.serial} at {SIZE[0]} x {SIZE[1]}, Mono8. Press Enter in the preview to start.", flush=True)
            exposure = camera.metadata["exposure_time_us"]
            gain = camera.metadata["gain_db"]
            armed = False
            pause_until = 0.0
            first_frame = True
            for (folder, poses, edges), views in zip(sets, saved):
                reference, stable_since, stable_frames = {}, 0.0, 0
                while len(views) < len(poses):
                    gray = read_gray(camera)
                    low, median, high = brightness(gray)
                    level = f"{exposure:g} us, {gain:g} dB | gray p01/median/p99: {low}/{median}/{high} of 255"
                    if first_frame:
                        print(f"First camera frame: {level}", flush=True)
                        first_frame = False
                    target_edges = edges.get(len(views) + 1, ())
                    corners, issue = inspect_frame(detector, board, gray, target_edges)
                    if verification is not None and not issue:
                        objects = board.getChessboardCorners()[list(corners)]
                        pixels = np.array(list(corners.values()), dtype=np.float32)
                        _, _, angle, distance_mm = check_pose(board, objects, pixels, verification)
                        level = f"Estimated board pose: {angle:.1f} deg | {distance_mm:.0f} mm from camera"
                        min_angle, max_angle = (0, 12) if not views else (20, 40)
                        if not 300 <= distance_mm <= 400:
                            direction = "closer" if distance_mm > 400 else "farther back"
                            issue = f"Move the board {direction} (target 300-400 mm)."
                        elif not min_angle <= angle <= max_angle:
                            direction = "less" if angle > max_angle else "more"
                            issue = f"Tilt the board {direction} (target {min_angle}-{max_angle} degrees)."
                    if high <= 20 and issue:
                        issue = "Frame is dark. Check lens cap, open iris or add light."
                    now = monotonic()
                    status = issue
                    if not armed and not issue:
                        status = "Check focus and exposure. Press Enter to start, or Esc to exit."
                    elif not armed:
                        status = issue
                    elif now < pause_until:
                        status = "Saved. Move to the next pose."
                    elif not issue:
                        prior = (view for group in saved for view in group)
                        if any(distance(corners, view) < 28 for view in prior):
                            status = "Move or tilt the board farther from earlier poses."
                        else:
                            if distance(corners, reference) > 6:
                                reference, stable_since, stable_frames = corners, now, 0
                            stable_frames += 1
                            status = "Hold the board still."
                            if now - stable_since >= 0.85 and stable_frames >= 4:
                                number = max((int(p.stem) for p in folder.glob("*.png")
                                              if p.stem.isdecimal()), default=0) + 1
                                path = folder / f"{number:03d}.png"
                                ok, png = cv2.imencode(".png", gray)
                                if not ok:
                                    raise RuntimeError("Could not encode the camera frame as PNG.")
                                with path.open("xb") as stream:
                                    stream.write(png.tobytes())
                                views.append(corners)
                                print(f"Saved {folder.name}/{path.name} ({len(corners)} corners)", flush=True)
                                pause_until = now + 1.35
                                reference = {}
                                continue
                    if status != "Hold the board still.":
                        reference = {}
                    group = "Verification" if verification is not None else folder.name.title()
                    title = f"{group} view {len(views) + 1} of {len(poses)}"
                    key = show_preview(gray, corners, title, poses[len(views)], status, level)
                    if key == 27 or cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                        return False
                    if key in (10, 13):
                        armed = True
    except genicam.GenericException as exc:
        raise RuntimeError(f"Basler {args.serial}: {exc}") from exc
    finally:
        cv2.destroyAllWindows()
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default="42007093")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--board", default=BOARD)
    parser.add_argument("--exposure-us", type=float, default=5000)
    parser.add_argument("--gain-db", type=float, default=0)
    parser.add_argument("--fps", type=int, choices=range(1, 24), default=10,
                        metavar="1-23", help="Camera frame rate limit (default: 10). Use a lower rate for long exposures.")
    parser.add_argument("--train-count", type=int, choices=range(25, 31), default=27)
    parser.add_argument("--check-count", type=int, choices=range(5, 11), default=7)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fit-only", action="store_true", help="Fit existing images without opening the camera")
    mode.add_argument("--diagnose", action="store_true", help="Save one original camera frame and print pixel levels without opening a window")
    mode.add_argument("--verify-intrinsics", type=Path,
                      help="Capture seven fresh working-range checks against an existing intrinsics JSON")
    parser.add_argument("--verification-run", type=int, default=1,
                        help="Use a separate verification folder for a new run (default: 1)")
    args = parser.parse_args()
    if not args.serial.isdecimal():
        parser.error("--serial must contain digits only")
    if args.verification_run < 1 or (args.verification_run != 1 and not args.verify_intrinsics):
        parser.error("--verification-run requires --verify-intrinsics and a positive run number")
    if args.diagnose:
        return diagnose(args)
    if not args.fit_only:
        board = reconstruct_board(load_manifest(str(args.manifest)), args.board)
        verification = None
        verification_folder = None
        if args.verify_intrinsics:
            data = json.loads(args.verify_intrinsics.read_text())
            if data.get("camera_provider") != "basler" or data.get("camera_serial") != args.serial:
                raise ValueError("The intrinsics file must match the selected Basler serial.")
            verification = opencv_calibration(data, SIZE)
            folder_name = f"verify-{args.verify_intrinsics.stem}"
            if args.verification_run > 1:
                folder_name += f"-run-{args.verification_run}"
            verification_folder = DATA / folder_name
        if not capture(args, board, verification, verification_folder):
            print("Capture paused. Run the same command to resume.")
            return 0
        if verification is not None:
            return review_verification(verification_folder, board, verification, args.verify_intrinsics)
    base = ROOT / ".ui-data" / "calibrations" / f"basler-{args.serial}-intrinsics.json"
    output, revision = base, 1
    while output.exists():
        revision += 1
        output = base.with_stem(f"{base.stem}-{revision}")
    print("Fitting intrinsics and checking the held-out images...", flush=True)
    return subprocess.run([sys.executable, str(ROOT / "tools" / "basler_intrinsics.py"),
                           "--images", str(DATA / "train"), "--check", str(DATA / "check"),
                           "--serial", args.serial, "--manifest", str(args.manifest),
                           "--board", args.board, "--output", str(output)], check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError, cv2.error) as exc:
        sys.exit(f"Capture failed: {exc}")
