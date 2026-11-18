#!/usr/bin/env python3
"""Manually capture native Basler encoder-plate views for offline intrinsics."""
import argparse
import json
from pathlib import Path
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_basler_intrinsics import BaslerCamera, SIZE, brightness, camera_config, read_gray
from encoder_intrinsics import detect_lattice, square_centres


def save_frame(folder, gray):
    if gray.dtype != np.uint8 or gray.ndim != 2:
        raise ValueError("Save only original Mono8 images")
    number = max((int(p.stem) for p in folder.glob("*.png") if p.stem.isdecimal()), default=0) + 1
    path = folder / f"{number:03d}.png"
    ok, png = cv2.imencode(".png", gray)
    if not ok:
        raise RuntimeError("Could not encode camera frame")
    with path.open("xb") as stream:
        stream.write(png.tobytes())
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="Actual camera serial number")
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--split", choices=("train", "check"), required=True)
    parser.add_argument("--pitch-mm", type=float, required=True)
    parser.add_argument("--exposure-us", type=float, default=10000)
    parser.add_argument("--gain-db", type=float, default=0)
    parser.add_argument("--fps", type=int, choices=range(1, 24), default=5)
    parser.add_argument("--count", type=int)
    args = parser.parse_args(argv)
    if (not args.serial.isdecimal() or not np.isfinite(args.pitch_mm) or args.pitch_mm <= 0
            or not np.isfinite(args.exposure_us) or not 0 < args.exposure_us <= 20000
            or not np.isfinite(args.gain_db) or not 0 <= args.gain_db <= 48
            or (args.count is not None and args.count <= 0)):
        parser.error("Use numeric serial, positive finite pitch/count, exposure 0-20000 us and gain 0-48 dB")
    count = args.count if args.count is not None else (25 if args.split == "train" else 5)
    folder = args.capture_dir / args.split
    if folder.exists():
        raise ValueError(f"{folder} exists; choose a fresh capture split")
    window = "Encoder plate: SPACE review, Enter save, R retake, Esc quit"
    preview_size = (900, round(900 * SIZE[1] / SIZE[0]))
    saved = 0
    try:
        with BaslerCamera(camera_config(args)) as camera:
            profile = dict(camera.metadata, nominal_cell_pitch_mm=args.pitch_mm, target="encoder_plain_squares")
            profile_path = args.capture_dir / "capture-profile.json"
            args.capture_dir.mkdir(parents=True, exist_ok=True)
            if profile_path.exists():
                if json.loads(profile_path.read_text()) != profile:
                    raise ValueError("Camera settings or pitch changed; choose a fresh capture directory")
            else:
                with profile_path.open("x", encoding="utf-8") as stream:
                    json.dump(profile, stream, indent=2, allow_nan=False)
                    stream.write("\n")
            folder.mkdir()
            cv2.namedWindow(window, cv2.WINDOW_NORMAL)
            while saved < count:
                gray = read_gray(camera)
                issue = ""
                try:
                    corners, _ = detect_lattice(gray)
                except ValueError as exc:
                    issue = str(exc)
                    corners = np.empty((0, 4, 2))
                centres = square_centres(corners)
                preview = cv2.cvtColor(cv2.resize(gray, preview_size), cv2.COLOR_GRAY2BGR)
                for quad in corners:
                    polygon = np.rint(quad * (np.array(preview_size) / SIZE)).astype(np.int32)
                    cv2.polylines(preview, [polygon], True, (80, 220, 90), 1)
                low, median, high = brightness(gray)
                status = f"{args.split} {saved}/{count}; {len(centres)} squares; gray {low}/{median}/{high}"
                cv2.putText(preview, status, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 230, 255), 1)
                cv2.putText(preview, issue or "Hold still; SPACE freezes for review", (12, 48),
                            cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 230, 255), 1)
                cv2.imshow(window, preview)
                key = cv2.waitKey(1) & 0xFF
                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1 or key in (27, ord("q")):
                    break
                if key == 32 and not issue:
                    cv2.putText(preview, "REVIEW: Enter saves; R retakes; Esc quits", (12, 72),
                                cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 230, 255), 1)
                    cv2.imshow(window, preview)
                    while True:
                        key = cv2.waitKey(30) & 0xFF
                        if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                            key = 27
                        if key in (10, 13, 27, ord("r"), ord("R")):
                            break
                    if key == 27:
                        break
                    if key in (10, 13):
                        path = save_frame(folder, gray)
                        saved += 1
                        print(f"Saved {path}: {len(centres)} indexed centres", flush=True)
    finally:
        cv2.destroyAllWindows()
    print(f"Saved {saved}/{count} {args.split} images. Lens focus/aperture and plate flatness require operator checks.")
    return 0 if saved == count else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError, cv2.error) as exc:
        sys.exit(f"Capture failed: {exc}")
