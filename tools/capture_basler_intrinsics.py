#!/usr/bin/env python3
"""Guide and confirm Basler ChArUco capture and fit the resulting intrinsics."""
import argparse
from dataclasses import dataclass
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
PREVIEW = (672, 562)
SIDEBAR = 416
BLUE = (255, 201, 95)
GREEN = (105, 230, 133)
AXES = (("left", "right"), ("top", "bottom"))


# Directions are relative to the live image, with positive x right and positive y down.
CENTRE = (0, 0)
LEFT, RIGHT, TOP, BOTTOM = (-1, 0), (1, 0), (0, -1), (0, 1)
UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT, LOWER_RIGHT = (-1, -1), (1, -1), (-1, 1), (1, 1)
CLOSER, FARTHER = (300, 340), (365, 400)


@dataclass(frozen=True)
class Target:
    region: tuple[int, int]
    nearer: tuple[int, int]
    distance_mm: tuple[int, int] = (300, 400)
    borders: tuple[str, ...] = ()

    @property
    def tilt_deg(self):
        return (0, 12) if self.nearer == CENTRE else (20, 40)


VERIFY_POSES = (
    Target(CENTRE, CENTRE),
    Target(CENTRE, LEFT),
    Target(CENTRE, RIGHT),
    Target(LEFT, TOP, borders=("left",)),
    Target(RIGHT, BOTTOM, borders=("right",)),
    Target(TOP, RIGHT, borders=("top",)),
    Target(BOTTOM, LEFT, borders=("bottom",)),
)
TRAIN_POSES = (
    VERIFY_POSES[0], VERIFY_POSES[3], VERIFY_POSES[1], VERIFY_POSES[2],
    VERIFY_POSES[5], VERIFY_POSES[4], VERIFY_POSES[6],
    Target(CENTRE, TOP),
    Target(CENTRE, BOTTOM),
    Target(UPPER_LEFT, TOP),
    Target(UPPER_RIGHT, RIGHT),
    Target(LOWER_LEFT, LEFT),
    Target(LOWER_RIGHT, BOTTOM),
    Target(LEFT, TOP, CLOSER, ("left",)),
    Target(TOP, RIGHT, FARTHER, ("top",)),
    Target(CENTRE, LEFT, CLOSER),
    Target(CENTRE, RIGHT, FARTHER),
    Target(CENTRE, TOP, CLOSER),
    Target(CENTRE, BOTTOM, FARTHER),
    Target(UPPER_LEFT, RIGHT, FARTHER),
    Target(UPPER_RIGHT, TOP, FARTHER),
    Target(LOWER_LEFT, BOTTOM, FARTHER),
    Target(LOWER_RIGHT, LEFT, FARTHER),
    Target(TOP, UPPER_LEFT, FARTHER),
    Target(BOTTOM, LOWER_RIGHT, FARTHER),
    Target(LEFT, LOWER_LEFT, FARTHER, ("left",)),
    Target(RIGHT, UPPER_RIGHT, FARTHER, ("right",)),
    Target(UPPER_LEFT, RIGHT, borders=("left", "top")),
    Target(UPPER_RIGHT, BOTTOM, borders=("right", "top")),
    Target(LOWER_LEFT, TOP, borders=("left", "bottom")),
)
CHECK_POSES = (
    Target(LEFT, UPPER_RIGHT, CLOSER),
    Target(RIGHT, LOWER_LEFT, CLOSER),
    Target(UPPER_RIGHT, LEFT, CLOSER),
    Target(LOWER_LEFT, RIGHT, CLOSER),
    Target(UPPER_LEFT, BOTTOM, CLOSER),
    Target(LOWER_RIGHT, TOP, CLOSER),
    Target(CENTRE, UPPER_LEFT, FARTHER),
    Target(LEFT, RIGHT, borders=("left",)),
    Target(RIGHT, LEFT, borders=("right",)),
    Target(TOP, BOTTOM, borders=("top",)),
)


def distance(first, second):
    common = sorted(first.keys() & second.keys())
    if len(common) < 12:
        return float("inf")
    offsets = np.array([first[i] - second[i] for i in common])
    return float(np.sqrt(np.mean(np.sum(offsets**2, axis=1))))


def board_boundary(board):
    width, height = np.array(board.getChessboardSize()) * board.getSquareLength()
    return np.float32([[0, 0, 0], [width, 0, 0], [width, height, 0], [0, height, 0]])


def board_outline(boundary, objects, pixels):
    homography, _ = cv2.findHomography(objects[:, :2].copy(), pixels.reshape(-1, 2))
    if homography is None:
        return None, "Show the whole board at a gentler angle."
    outer = cv2.perspectiveTransform(boundary[None, :, :2].copy(), homography)[0]
    if not (np.isfinite(outer).all() and np.all(outer >= 5) and np.all(outer <= np.array(SIZE) - 6)):
        return None, "Keep the whole pattern inside the frame."
    return outer, ""


def inspect_frame(detector, board, gray, boundary):
    found = detector.detect(gray)
    corners = {i: xy.ravel() for i, xy in zip(found.charuco_ids, found.charuco_corners)}
    total = len(board.getChessboardCorners())
    if len(corners) < max(12, int(np.ceil(total * 0.8))):
        return corners, None, f"Corners {len(corners)}/{total}. Show the full board and check focus."
    objects = board.getChessboardCorners()[list(corners)]
    outer, issue = board_outline(boundary, objects, np.array(list(corners.values())))
    return corners, outer, issue


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


def edge_depths(boundary, outer, rotation, translation):
    orientation, _ = cv2.Rodrigues(rotation)
    depth = (boundary @ orientation[2] + translation[2, 0]) * 1000
    # Follow the physical corner order, starting with the top edge in the image.
    top = np.argmin(outer[:, 1] + np.roll(outer[:, 1], -1))
    outer, depth, boundary = (np.roll(value, -top, axis=0) for value in (outer, depth, boundary))
    tl, tr, br, bl = depth
    differences_mm = np.array([(tl + bl - tr - br) / 2, (tl + tr - bl - br) / 2])
    lengths_mm = 1000 * np.linalg.norm(boundary[1:3] - boundary[:2], axis=1)
    return outer, differences_mm, differences_mm / lengths_mm


def position_name(region):
    x, y = region
    horizontal = AXES[0][(x + 1) // 2] if x else ""
    vertical = ("upper" if y < 0 else "lower") if x else ("top" if y < 0 else "bottom")
    return " ".join(part for part in (vertical if y else "", horizontal) if part) or "centre"


def tip_instruction(direction):
    x, y = direction
    if not (x or y):
        return "Face the board square to the lens"
    if x and y:
        return f"Tip the {position_name(direction)} corner toward the lens"
    return f"Tip the {position_name(direction)} edge toward the lens"


def target_issue(target, outer, slope, angle, distance_mm, metric):
    x, y = (outer / SIZE).T
    gaps = {"left": x.min(), "right": 1 - x.max(), "top": y.min(), "bottom": 1 - y.max()}
    borders = [edge for edge in target.borders if gaps[edge] > 0.12]
    if borders:
        return f"Move the whole board closer to the {' and '.join(borders)} of the preview."
    for position, expected in zip((x.mean(), y.mean()), target.region):
        if ((expected < 0 and position > 0.43) or (expected > 0 and position < 0.57)
                or (expected == 0 and not 0.34 <= position <= 0.66)):
            return f"Move the whole board toward the {position_name(target.region)} guide."
    dx, dy = outer[1] - outer[0]
    if dx <= 0 or abs(dy) > 0.52 * dx:
        return "Keep the top edge roughly level in the preview."
    near, far = target.distance_mm
    if metric and not near <= distance_mm <= far:
        direction = "closer" if distance_mm > far else "farther back"
        return f"Move the whole board {direction} ({near}-{far} mm)."
    low, high = target.tilt_deg
    if not low <= angle <= high:
        direction = "less" if angle > high else "more"
        return f"Tilt {direction} ({low}-{high} degrees)."
    if target.nearer != CENTRE:
        # The signed depth gradient must point within 30 degrees of the requested direction.
        alignment = np.dot(slope, target.nearer)
        minimum = np.cos(np.pi / 6) * np.linalg.norm(slope) * np.linalg.norm(target.nearer)
        if alignment < minimum:
            return tip_instruction(target.nearer) + "."
    return ""


def review_verification(folder, board, calibration, source):
    views = observations(folder, board, SIZE)
    if len(views) != len(VERIFY_POSES):
        raise ValueError(f"Expected {len(VERIFY_POSES)} usable verification photos, found {len(views)}.")
    boundary = board_boundary(board)
    checks = []
    for (path, objects, pixels), target in zip(views, VERIFY_POSES):
        rotation, translation, angle, distance_mm = check_pose(board, objects, pixels, calibration)
        rms = reprojection_error(objects, pixels, *calibration, rotation, translation)
        outer, issue = board_outline(boundary, objects, pixels)
        if not issue:
            outer, _, slope = edge_depths(boundary, outer, rotation, translation)
            issue = target_issue(target, outer, slope, angle, distance_mm, metric=True)
        checks.append({"image": str(path), "reprojection_rmse_px": rms,
                       "tilt_deg": angle, "distance_mm": distance_mm, "pose_issue": issue or None})
        print(f"{path.name}: {rms:.3f} px RMS, {angle:.1f} deg, {distance_mm:.0f} mm", flush=True)
        if issue:
            print(f"  Pose: {issue}", flush=True)
    passed = all(check["reprojection_rmse_px"] < 0.5 and not check["pose_issue"] for check in checks)
    report = {"intrinsics": str(source.resolve()), "checks": checks, "threshold_px": 0.5,
              "passed": passed, "working_distance_mm": [300, 400], "tilted_views_deg": [20, 40]}
    path = folder / "verification.json"
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"Working-range reprojection check: {'PASS' if passed else 'FAIL'}. Report: {path}")
    return 0 if passed else 1


def label(canvas, text, x, y, scale=0.53, colour=(240, 242, 248)):
    cv2.putText(canvas, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, colour, 1, cv2.LINE_AA)


def guide_position(target):
    return np.array([0.5, 0.5]) + np.array(target.region) * [0.29, 0.25]


def draw_nearer_edges(image, polygon, nearer, colour=BLUE):
    x, y = nearer
    if x:
        a, b = (0, 3) if x < 0 else (1, 2)
        cv2.line(image, tuple(polygon[a]), tuple(polygon[b]), colour, 4, cv2.LINE_AA)
    if y:
        a, b = (0, 1) if y < 0 else (3, 2)
        cv2.line(image, tuple(polygon[a]), tuple(polygon[b]), colour, 4, cv2.LINE_AA)


def draw_tilt_guide(panel, y, axis, direction):
    title = "Top view (left/right tilt)" if axis == 0 else "Side view (top/bottom tilt)"
    label(panel, title, 20, y)
    middle = y + 50
    # The lens faces right; moving a board endpoint left brings it toward the lens.
    cv2.rectangle(panel, (30, middle - 13), (75, middle + 13), (139, 147, 163), -1)
    cv2.rectangle(panel, (75, middle - 7), (93, middle + 7), (188, 194, 205), -1)
    cv2.line(panel, (94, middle), (203, middle), (106, 114, 132), 1, cv2.LINE_AA)
    first = (211 + 22 * direction, middle - 30)
    last = (211 - 22 * direction, middle + 30)
    cv2.line(panel, first, last, (240, 242, 248), 4, cv2.LINE_AA)
    for index, point in enumerate((first, last)):
        nearer = direction == (-1, 1)[index]
        colour = BLUE if nearer else (224, 228, 237)
        cv2.circle(panel, point, 5, colour, -1, cv2.LINE_AA)
        label(panel, AXES[axis][index].title(), point[0] + 13, point[1] + 5, colour=colour)
    label(panel, "Lens", 32, middle + 33, scale=0.44)


def render_preview(gray, corners, title, target, status, level="", outer=None, ready=False):
    preview = cv2.resize(gray, PREVIEW, interpolation=cv2.INTER_AREA)
    preview = cv2.cvtColor(preview, cv2.COLOR_GRAY2BGR)
    scale = np.array(PREVIEW) / SIZE
    for point in corners.values():
        cv2.circle(preview, tuple(np.rint(point * scale).astype(int)), 2, GREEN, -1)
    centre = tuple(np.rint(guide_position(target) * PREVIEW).astype(int))
    cv2.drawMarker(preview, centre, BLUE, cv2.MARKER_CROSS, 19, 1, cv2.LINE_AA)
    if outer is not None:
        outline = np.rint(outer * scale).astype(np.int32)
        cv2.polylines(preview, [outline], True, GREEN if ready else (190, 195, 204), 2, cv2.LINE_AA)
        draw_nearer_edges(preview, outline, target.nearer)

    panel = np.full((PREVIEW[1], SIDEBAR, 3), (39, 33, 27), dtype=np.uint8)
    label(panel, "TARGET IN CAMERA PREVIEW", 20, 27, scale=0.57)
    label(panel, "Blue edge goes toward the lens", 20, 50, colour=BLUE)
    origin, size = np.array([20, 65]), np.array([242, 122])
    cv2.rectangle(panel, tuple(origin), tuple(origin + size), (57, 52, 49), -1)
    cv2.rectangle(panel, tuple(origin), tuple(origin + size), (129, 121, 115), 1)
    x, y = np.rint(origin + guide_position(target) * size).astype(int)
    polygon = np.array([[x - 20, y - 27], [x + 20, y - 27], [x + 20, y + 27], [x - 20, y + 27]])
    cv2.fillConvexPoly(panel, polygon, (209, 216, 229))
    for row in range(4):
        for column in range(3):
            if (row + column) % 2 == 0:
                a = (x - 18 + 12 * column, y - 24 + 12 * row)
                cv2.rectangle(panel, a, (a[0] + 11, a[1] + 11), (65, 60, 57), -1)
    draw_nearer_edges(panel, polygon, target.nearer)
    position = position_name(target.region)
    label(panel, "Centre the board" if position == "centre" else f"Move the board near the {position}", 20, 215)
    label(panel, tip_instruction(target.nearer), 20, 241, scale=0.48, colour=BLUE)
    draw_tilt_guide(panel, 278, 0, target.nearer[0])
    draw_tilt_guide(panel, 393, 1, target.nearer[1])
    near, far = target.distance_mm
    low, high = target.tilt_deg
    label(panel, f"Target  {near}-{far} mm  |  {low}-{high} deg", 20, 535)

    header = np.full((108, PREVIEW[0] + SIDEBAR, 3), (30, 25, 20), dtype=np.uint8)
    label(header, title, 18, 28, scale=0.77)
    label(header, status, 18, 61, scale=0.62, colour=GREEN if ready else (240, 242, 248))
    label(header, level, 18, 89, colour=(192, 198, 211))
    return np.vstack((header, np.hstack((preview, panel))))


def preview_key(delay):
    key = cv2.waitKey(delay) & 0xFF
    try:
        if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
            return 27
    except cv2.error:
        return 27
    return key


def confirm_preview():
    while True:
        key = preview_key(30)
        if key in (10, 13, 32, 27, ord("r"), ord("R")):
            return key


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
    args.capture_dir.mkdir(parents=True, exist_ok=True)
    path = args.capture_dir / "preview-diagnostic.png"
    if not cv2.imwrite(str(path), gray):
        raise RuntimeError(f"Could not save diagnostic frame to {path}.")
    print(f"Saved the original frame to {path}.")
    if high <= 20:
        print("The camera frame is very dark. Check the lens cap and iris, light the board, or increase exposure.")
    return 0


def capture(args, board, pose_calibration, verification_folder=None):
    detector = CharucoDetector(board)
    boundary = board_boundary(board)
    sets = [(args.capture_dir / "train", TRAIN_POSES[:args.train_count]),
            (args.capture_dir / "check", CHECK_POSES[:args.check_count])]
    if verification_folder is not None:
        sets = [(verification_folder, VERIFY_POSES)]
    for folder, _ in sets:
        folder.mkdir(parents=True, exist_ok=True)
    saved = [existing_poses(folder, board) for folder, _ in sets]
    if all(len(views) >= len(poses) for views, (_, poses) in zip(saved, sets)):
        return True

    metric = pose_calibration is not None
    if not metric:
        width, height = SIZE
        focal = 1.8 * width
        pose_calibration = (np.array([[focal, 0, width / 2], [0, focal, height / 2], [0, 0, 1]]),
                            np.zeros(5))
        print("Using approximate pose guidance. Supply --pose-intrinsics to check the distance.", flush=True)
    check_gige_access()
    print(f"Opening Basler {args.serial}. Close pylon Viewer if it is holding the camera.", flush=True)
    from pypylon import genicam

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW, PREVIEW[0] + SIDEBAR, PREVIEW[1] + 108)
    try:
        blank = render_preview(np.zeros(SIZE[::-1], dtype=np.uint8), {}, f"Basler {args.serial}",
                               TRAIN_POSES[0], "Waiting for the first camera frame.")
        cv2.imshow(WINDOW, blank)
        if preview_key(1) == 27:
            return False
        with BaslerCamera(camera_config(args)) as camera:
            profile_dir = verification_folder or args.capture_dir
            profile_path = profile_dir / "capture-profile.json"
            profile = dict(camera.metadata, board=args.board, manifest=str(args.manifest.resolve()))
            source = args.verify_intrinsics or args.pose_intrinsics
            profile["pose_intrinsics"] = str(source.resolve()) if source else None
            profile["confirmation"] = "manual"
            if profile_path.exists() and json.loads(profile_path.read_text()) != profile:
                if any(saved):
                    raise ValueError("Camera, board or pose guidance differs from the previous capture. "
                                     "Choose a fresh folder with --capture-dir.")
                profile_path.write_text(json.dumps(profile, indent=2) + "\n")
            elif not profile_path.exists():
                if any(saved):
                    raise ValueError("Existing photos have no capture profile. Use --capture-dir for a fresh capture.")
                profile_path.write_text(json.dumps(profile, indent=2) + "\n")
            print(f"Opened {camera.serial} at {SIZE[0]} x {SIZE[1]}, Mono8. "
                  "Hold the target pose to review a photo, then press Enter to save.", flush=True)
            exposure = camera.metadata["exposure_time_us"]
            gain = camera.metadata["gain_db"]
            first_frame = True
            for (folder, poses), views in zip(sets, saved):
                group = "Verification" if verification_folder is not None else folder.name.title()
                reference, stable_since, stable_frames = {}, 0.0, 0
                while len(views) < len(poses):
                    gray = read_gray(camera)
                    low, median, high = brightness(gray)
                    level = f"{exposure:g} us, {gain:g} dB | gray p01/median/p99: {low}/{median}/{high} of 255"
                    if first_frame:
                        print(f"First camera frame: {level}", flush=True)
                        first_frame = False
                    target = poses[len(views)]
                    corners, outer, issue = inspect_frame(detector, board, gray, boundary)
                    if not issue:
                        objects = board.getChessboardCorners()[list(corners)]
                        pixels = np.array(list(corners.values()), dtype=np.float32)
                        try:
                            rotation, translation, angle, distance_mm = check_pose(
                                board, objects, pixels, pose_calibration
                            )
                        except (ValueError, cv2.error):
                            issue = "Show the whole pattern so the pose can be estimated."
                            outer = None
                        else:
                            outer, depths, slope = edge_depths(boundary, outer, rotation, translation)
                            issue = target_issue(target, outer, slope, angle, distance_mm, metric)
                            edges = " | ".join(
                                f"{names[int(depth > 0)].title()} {abs(depth):.0f} mm nearer"
                                for names, depth in zip(AXES, depths)
                            )
                            prefix = "Live" if metric else "Approx."
                            level = f"{prefix}  {distance_mm:.0f} mm | {angle:.1f} deg | {edges}"
                    if high <= 20 and issue:
                        issue = "Frame is dark. Check lens cap, open iris or add light."
                    if not issue and any(distance(corners, view) < 28 for group_views in saved for view in group_views):
                        issue = "Change the position or tilt slightly within the target."
                    ready = False
                    if issue:
                        reference = {}
                        status = issue
                    else:
                        now = monotonic()
                        if distance(corners, reference) > 6:
                            reference, stable_since, stable_frames = corners, now, 0
                        stable_frames += 1
                        ready = now - stable_since >= 0.65 and stable_frames >= 4
                        status = ("Pose matched. Enter saves  |  R retakes  |  Esc pauses" if ready
                                  else "Pose matched. Hold still to review the photo.")
                    title = f"{group} view {len(views) + 1} of {len(poses)}"
                    cv2.imshow(WINDOW, render_preview(gray, corners, title, target, status, level, outer, ready))
                    key = confirm_preview() if ready else preview_key(1)
                    if key == 27:
                        return False
                    if not ready:
                        continue
                    reference = {}
                    if key in (ord("r"), ord("R")):
                        continue
                    number = max((int(p.stem) for p in folder.glob("*.png")
                                  if p.stem.isdecimal()), default=0) + 1
                    path = folder / f"{number:03d}.png"
                    ok, png = cv2.imencode(".png", gray)
                    if not ok:
                        raise RuntimeError("Could not encode the camera frame as PNG.")
                    with path.open("xb") as stream:
                        stream.write(png.tobytes())
                    views.append(corners)
                    print(f"Confirmed {folder.name}/{path.name} ({len(corners)} corners). {level}", flush=True)
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
    parser.add_argument("--capture-dir", type=Path, default=DATA,
                        help="Folder for a fresh capture or a session to resume")
    parser.add_argument("--pose-intrinsics", type=Path,
                        help="Existing Basler intrinsics for distance and tilt guidance during training")
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
    if args.pose_intrinsics and (args.verify_intrinsics or args.fit_only or args.diagnose):
        parser.error("--pose-intrinsics is for guided training capture")
    if args.diagnose:
        return diagnose(args)
    if not args.fit_only:
        board = reconstruct_board(load_manifest(str(args.manifest)), args.board)
        pose_calibration = None
        verification_folder = None
        pose_source = args.verify_intrinsics or args.pose_intrinsics
        if pose_source:
            data = json.loads(pose_source.read_text())
            if data.get("camera_provider") != "basler" or data.get("camera_serial") != args.serial:
                raise ValueError("The intrinsics file must match the selected Basler serial.")
            pose_calibration = opencv_calibration(data, SIZE)
        if args.verify_intrinsics:
            folder_name = f"verify-{args.verify_intrinsics.stem}"
            if args.verification_run > 1:
                folder_name += f"-run-{args.verification_run}"
            verification_folder = args.capture_dir / folder_name
        if not capture(args, board, pose_calibration, verification_folder):
            print("Capture paused. Run the same command to resume.")
            return 0
        if verification_folder is not None:
            return review_verification(verification_folder, board, pose_calibration, args.verify_intrinsics)
    base = ROOT / ".ui-data" / "calibrations" / f"basler-{args.serial}-intrinsics.json"
    output, revision = base, 1
    while output.exists():
        revision += 1
        output = base.with_stem(f"{base.stem}-{revision}")
    print("Fitting intrinsics and checking the held-out images...", flush=True)
    return subprocess.run([sys.executable, str(ROOT / "tools" / "basler_intrinsics.py"),
                           "--images", str(args.capture_dir / "train"), "--check", str(args.capture_dir / "check"),
                           "--serial", args.serial, "--manifest", str(args.manifest),
                           "--board", args.board, "--output", str(output)], check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError, cv2.error) as exc:
        sys.exit(f"Capture failed: {exc}")
