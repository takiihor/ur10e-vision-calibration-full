#!/usr/bin/env python3
"""Offline intrinsics from plain bright and dark squares on a Vision Encoder plate."""
import argparse
from collections import deque
import json
from pathlib import Path
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from basler_intrinsics import opencv_calibration


def square_centres(corners):
    """Intersect diagonals; averaging four corners is biased by perspective."""
    corners = np.asarray(corners, np.float64).reshape(-1, 4, 2)
    if not len(corners):
        return np.empty((0, 2))
    homogeneous = np.concatenate((corners, np.ones((*corners.shape[:2], 1))), axis=2)
    first = np.cross(homogeneous[:, 0], homogeneous[:, 2])
    second = np.cross(homogeneous[:, 1], homogeneous[:, 3])
    centres = np.cross(first, second)
    if np.any(np.abs(centres[:, 2]) < 1e-9):
        raise ValueError("Degenerate square diagonals")
    return centres[:, :2] / centres[:, 2:]


def edge_samples(quad):
    """Native sample windows shared by edge fitting and saturation rejection."""
    for start, end in zip(quad, np.roll(quad, -1, axis=0)):
        tangent = end - start
        length = np.linalg.norm(tangent)
        normal = np.array([-tangent[1], tangent[0]]) / length
        along = np.linspace(.3, .7, max(8, int(length)))
        offsets = np.arange(-min(8, length * .35), min(8, length * .35) + .125, .25)
        samples = start + along[:, None, None] * tangent + offsets[None, :, None] * normal
        yield start, tangent, normal, along, offsets, samples


def fitted_square(gray, quad, peak=False):
    lines, errors = [], []
    for start, tangent, normal, along, offsets, samples in edge_samples(quad):
        levels = cv2.remap(gray, samples[:, :, 0].astype(np.float32),
                           samples[:, :, 1].astype(np.float32), cv2.INTER_LINEAR)
        gradients = np.maximum(np.diff(levels, axis=1), 0)
        if peak:
            # Follow one edge along the side, rather than switching to nearby frame seams.
            strongest = int(np.argmax(gradients.mean(axis=0)))
            if strongest < 6 or strongest >= gradients.shape[1] - 6:
                return None
            gradients *= np.abs(np.arange(gradients.shape[1]) - strongest) <= 6
        strength = gradients.sum(axis=1)
        if np.min(strength) < 8:
            return None
        position = (gradients @ ((offsets[:-1] + offsets[1:]) / 2)) / strength
        edge = start + along[:, None] * tangent + position[:, None] * normal
        vx, vy, x, y = cv2.fitLine(edge.astype(np.float32), cv2.DIST_L2, 0, .001, .001).ravel()
        line = np.array([-vy, vx, vy * x - vx * y])
        errors.extend(np.abs(edge @ line[:2] + line[2]))
        lines.append(line)
    if np.percentile(errors, 90) > .4 or np.max(errors) > .8:
        return None
    intersections = np.cross(np.roll(lines, 1, axis=0), lines)
    if np.any(np.abs(intersections[:, 2]) < .1):
        return None
    return intersections[:, :2] / intersections[:, 2:]


def detect_squares(gray, edge_windows=None):
    """Fit both polarities independently: their side lengths need not match."""
    if gray.ndim != 2 or gray.dtype != np.uint8:
        raise ValueError("Use original 8-bit Mono8 images")
    if int(gray.max()) - int(gray.min()) < 16:
        return np.empty((0, 4, 2))
    windows = ([], [])
    groups = (_bright_squares(gray, windows[0]), _bright_squares(255 - gray, windows[1]))
    widths = [np.linalg.norm(group - np.roll(group, -1, axis=1), axis=2).mean(axis=1)
              for group in groups]
    typical = max((np.percentile(w, 75) for w in widths if len(w)), default=0)
    # shortcut: plain widths differ by at most 2.5x, use a pitch model for larger ratios.
    keep = [w >= .4 * typical for w in widths]
    if edge_windows is not None:
        for search, mask in zip(windows, keep):
            edge_windows.extend(quad for quad, use in zip(search, mask) if use)
    return np.concatenate([group[mask] for group, mask in zip(groups, keep)])


def _bright_squares(gray, edge_windows):
    """Find solid light quadrilaterals and fit native grayscale edge lines."""
    values = gray.astype(np.float32)
    background = cv2.GaussianBlur(values, (0, 0), 32)
    contrast = np.sqrt(cv2.GaussianBlur((values - background) ** 2, (0, 0), 32))
    normalized = np.clip(128 + 40 * (values - background) / np.maximum(contrast, 1), 0, 255).astype(np.uint8)
    _, binary = cv2.threshold(normalized, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    squares, widths, windows = [], [], []
    levels = cv2.GaussianBlur(cv2.normalize(values, None, 0, 255, cv2.NORM_MINMAX), (0, 0), .6)
    for k, contour in enumerate(contours):
        if hierarchy[0, k, 3] != -1:
            continue
        area = cv2.contourArea(contour)
        if area < 40:
            continue
        child = hierarchy[0, k, 2]
        if child != -1 and cv2.contourArea(contours[child]) > .03 * area:
            continue
        quad = cv2.approxPolyDP(contour, .055 * cv2.arcLength(contour, True), True)
        if len(quad) != 4 or not cv2.isContourConvex(quad):
            continue
        quad = quad[:, 0].astype(np.float64)
        if cv2.contourArea(quad.astype(np.float32), oriented=True) < 0:
            quad = quad[::-1]
        lengths = np.linalg.norm(quad - np.roll(quad, -1, axis=0), axis=1)
        if lengths.min() < 6 or lengths.max() / lengths.min() > 1.8:
            continue
        if np.any(quad < 8) or np.any(quad > np.array(gray.shape[::-1]) - 9):
            continue
        transform = cv2.getPerspectiveTransform(quad.astype(np.float32),
                                                np.float32([[0, 0], [32, 0], [32, 32], [0, 32]]))
        patch = cv2.warpPerspective(values, transform, (33, 33))
        interior = patch[6:27, 6:27]
        rim = np.r_[patch[4:7, 6:27].ravel(), patch[26:29, 6:27].ravel(),
                    patch[6:27, 4:7].ravel(), patch[6:27, 26:29].ravel()]
        # A coded cell has a bright rim around a darker or textured interior.
        local_range = np.ptp(patch)
        if interior.mean() < rim.mean() - .02 * local_range or interior.std() > .12 * local_range:
            continue
        fitted = fitted_square(levels, quad)
        if fitted is not None:
            sides = np.linalg.norm(fitted - np.roll(fitted, -1, axis=0), axis=1)
            if sides.max() / sides.min() <= 1.5:
                squares.append(fitted)
                widths.append(float(np.mean(sides)))
                windows.append(quad)
    if not squares:
        return np.empty((0, 4, 2))
    # Code fragments can look square but are smaller than the plain-square population.
    widths = np.asarray(widths)
    typical = np.percentile(widths, 75)
    keep = (widths >= .65 * typical) & (widths <= 1.5 * typical)
    edge_windows.extend(quad for quad, use in zip(windows, keep) if use)
    return np.asarray(squares)[keep]


def nearest_indices(points, count):
    descriptors = np.ascontiguousarray(points, np.float32)
    matches = cv2.BFMatcher(cv2.NORM_L2).knnMatch(descriptors, descriptors, k=min(len(points), count + 1))
    return np.array([[m.trainIdx for m in row if m.trainIdx != n][:count]
                     for n, row in enumerate(matches)])


def index_lattice(centres, image_size):
    """Grow predicted integer links across gaps, checking cycles and local geometry."""
    points = np.asarray(centres, np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 12 or not np.isfinite(points).all():
        raise ValueError("Need at least 12 finite square centres")
    neighbours = nearest_indices(points, 16)
    distances = np.linalg.norm(points[neighbours] - points[:, None], axis=2)
    spacing = float(np.median(distances[:, 0]))
    if spacing <= 0 or distances.min() < .4 * spacing:
        raise ValueError("Duplicate or overlapping lattice points")
    seed = int(np.argmin(np.linalg.norm(points - np.array(image_size) / 2, axis=1)))
    vectors = points[neighbours[seed, :8]] - points[seed]
    options = []
    for a in range(len(vectors)):
        for b in range(a):
            lengths = np.linalg.norm(vectors[[a, b]], axis=1)
            cosine = abs(np.dot(vectors[a], vectors[b]) / np.prod(lengths))
            if np.all(np.abs(lengths / spacing - 1) < .3) and cosine < .35:
                options.append((cosine + np.sum(np.abs(lengths / spacing - 1)), a, b))
    if not options:
        raise ValueError("Cannot establish two lattice axes near the image centre")
    _, a, b = min(options)
    basis = vectors[[a, b]]
    if np.linalg.det(basis) < 0:
        basis = basis[::-1]
    links = []
    inverse_basis = np.linalg.inv(basis)
    for n in range(len(points)):
        near = neighbours[n]
        local = (points[near] - points[n]) @ inverse_basis
        steps = np.rint(local).astype(np.int32)
        row = []
        # shortcut: gaps span at most four steps, use local homographies for wider gaps.
        for other, predicted, step in zip(near, local, steps):
            tolerance = .32 if np.abs(step).sum() == 1 else .22
            if (n in neighbours[other] and np.max(np.abs(step)) <= 4
                    and np.any(step) and np.linalg.norm(predicted - step) < tolerance):
                row.append((int(other), step))
        links.append(row)
    indices = np.zeros((len(points), 2), np.int32)
    visited = {seed}
    pending = deque([seed])
    while pending:
        n = pending.popleft()
        for other, step in links[n]:
            if not any(back == n and np.array_equal(back_step, -step) for back, back_step in links[other]):
                raise ValueError("Non-reciprocal lattice neighbour")
            expected = indices[n] + step
            if other in visited:
                if not np.array_equal(indices[other], expected):
                    raise ValueError("Inconsistent lattice cycle")
            else:
                indices[other] = expected
                visited.add(other)
                pending.append(other)
    if len(visited) != len(points):
        raise ValueError("Cannot bridge lattice gap with unambiguous integer predictions")
    validate_lattice(points, indices, spacing)
    return indices


def validate_lattice(points, indices, spacing, neighbours=11):
    """Check fixed integer identities without assigning a new phase."""
    if len(np.unique(indices, axis=0)) != len(points):
        raise ValueError("Duplicate lattice indices")
    local_neighbours = nearest_indices(indices, neighbours)
    for n in range(len(points)):
        near = np.r_[n, local_neighbours[n]]
        homography, _ = cv2.findHomography(indices[near].astype(np.float32), points[near])
        if homography is None:
            raise ValueError("Degenerate local lattice")
        predicted = cv2.perspectiveTransform(indices[near, None].astype(np.float32), homography)[:, 0]
        if np.max(np.linalg.norm(predicted - points[near], axis=1)) > max(.5, .03 * spacing):
            raise ValueError("High local lattice homography residual")


def refine_predicted_square(polarities, homography, cell, spacing, edge_windows=None):
    """Measure four native edges at a predicted plain cell, including connected dark cells."""
    diamond = np.array([[0, -1], [1, 0], [0, 1], [-1, 0]], np.float32)
    predicted = cv2.perspectiveTransform(np.float32([[cell]]), homography)[0, 0]
    axes = cv2.perspectiveTransform(np.float32([[cell + [1, 0], cell + [0, 1]]]), homography)[0]
    local_spacing = np.linalg.norm(axes - predicted, axis=1)
    for polarity in polarities:
        for radius in (.28, .35, .42, .5):
            quad = cv2.perspectiveTransform((cell + radius * diamond)[None].astype(np.float32), homography)[0]
            if np.any(quad < 9) or np.any(quad > np.array(polarity.shape[::-1]) - 10):
                continue
            fitted = fitted_square(polarity, quad, peak=True)
            if fitted is None:
                continue
            if np.any(fitted < 8) or np.any(fitted > np.array(polarity.shape[::-1]) - 9):
                continue
            lengths = np.linalg.norm(fitted - np.roll(fitted, -1, axis=0), axis=1)
            if (lengths.min() < .28 * local_spacing.min() or lengths.max() > .85 * local_spacing.max()
                    or lengths.max() / lengths.min() > 1.5):
                continue
            centre = square_centres([fitted])[0]
            if np.linalg.norm(centre - predicted) > max(.5, .02 * spacing):
                continue
            transform = cv2.getPerspectiveTransform(fitted.astype(np.float32),
                                                    np.float32([[0, 0], [32, 0], [32, 32], [0, 32]]))
            patch = cv2.warpPerspective(polarity, transform, (33, 33))
            interior = patch[6:27, 6:27]
            rim = np.r_[patch[4:7, 6:27].ravel(), patch[26:29, 6:27].ravel(),
                        patch[6:27, 4:7].ravel(), patch[6:27, 26:29].ravel()]
            contrast = np.ptp(patch)
            if contrast < 8 or interior.std() > .12 * contrast or interior.mean() < rim.mean() - .02 * contrast:
                continue
            if edge_windows is not None:
                edge_windows.append(quad)
            return fitted
    return None


def detect_lattice(gray, diagnostics=None):
    """Seed confident squares, then predict and measure plain cells in the same phase."""
    windows = []
    candidates = detect_squares(gray, edge_windows=windows)
    points = square_centres(candidates)
    if len(points) < 12:
        raise ValueError("Need at least 12 confident seed squares")
    neighbours = nearest_indices(points, 16)
    distances = np.linalg.norm(points[neighbours] - points[:, None], axis=2)
    spacing = float(np.median(distances[:, 0]))
    unseen, components = set(range(len(points))), []
    while unseen:
        pending = [unseen.pop()]
        component = []
        while pending:
            n = pending.pop()
            component.append(n)
            for other in neighbours[n][distances[n] <= 4 * spacing]:
                if other in unseen:
                    unseen.remove(other)
                    pending.append(int(other))
        components.append(component)
    seed = max(components, key=len)
    # Sparse dark contours and distant white islands cannot establish one seed basis.
    dense = np.array([n for n in seed if 1 + np.sum(distances[n] <= 3 * spacing) >= 12])
    if not len(dense):
        raise ValueError("Need a dense local patch of confident seed squares")
    anchor = dense[np.argmin(np.linalg.norm(points[dense] - np.array(gray.shape[::-1]) / 2, axis=1))]
    seed = np.asarray(seed)[np.linalg.norm(points[seed] - points[anchor], axis=1) <= 6 * spacing]
    # Clipping can cut the only path to a candidate; grow to it after establishing the seed phase.
    remaining = set(seed)
    remaining.remove(anchor)
    pending = [anchor]
    while pending:
        n = pending.pop()
        for other in neighbours[n][distances[n] <= 4 * spacing]:
            if other in remaining:
                remaining.remove(other)
                pending.append(other)
    seed = np.array([n for n in seed if n not in remaining])
    seed_centre = points[anchor]
    corners = list(candidates[seed])
    windows = [windows[n] for n in seed]
    points = list(points[seed])
    indices = list(index_lattice(points, 2 * seed_centre))
    accepted = {tuple(cell) for cell in indices}
    levels = cv2.GaussianBlur(cv2.normalize(gray.astype(np.float32), None, 0, 255, cv2.NORM_MINMAX), (0, 0), .6)
    polarities = (levels, 255 - levels)
    # Only integer steps in this plain-square lattice are proposed; code cells have half-integer parity.
    # shortcut: growth crosses one missing cell; use wider supported neighbourhoods for larger gaps.
    steps = np.array([(x, y) for x in range(-2, 3) for y in range(-2, 3) if 0 < abs(x) + abs(y) <= 2])
    while True:
        cells = np.asarray(indices)
        pixels = np.asarray(points)
        targets = sorted({tuple(cell + step) for cell in cells for step in steps} - accepted)
        target_array = np.asarray(targets, np.float32)
        if not len(targets):
            break
        matches = cv2.BFMatcher(cv2.NORM_L2).knnMatch(target_array, cells.astype(np.float32), k=min(8, len(cells)))
        added = 0
        for target, near in zip(targets, matches):
            near = np.array([m.trainIdx for m in near])
            local_cells = cells[near].astype(np.float32)
            if np.linalg.norm(local_cells[0] - target) > 2 or np.linalg.matrix_rank(local_cells - local_cells.mean(axis=0)) < 2:
                continue
            homography, _ = cv2.findHomography(local_cells, pixels[near])
            if homography is None:
                continue
            predicted = cv2.perspectiveTransform(local_cells[:, None], homography)[:, 0]
            if np.max(np.linalg.norm(predicted - pixels[near], axis=1)) > max(.5, .03 * spacing):
                continue
            centre = cv2.perspectiveTransform(np.float32([[target]]), homography)[0, 0]
            if np.any(centre < 9) or np.any(centre > np.array(gray.shape[::-1]) - 10):
                continue
            fitted = refine_predicted_square(polarities, homography, np.asarray(target), spacing, windows)
            if fitted is None:
                continue
            corners.append(fitted)
            points.append(square_centres([fitted])[0])
            indices.append(np.array(target, np.int32))
            accepted.add(target)
            added += 1
        if not added:
            break
    points, indices = np.asarray(points), np.asarray(indices, np.int32)
    # Eight nearby cells keep the geometry check local under radial curvature.
    validate_lattice(points, indices, spacing, neighbours=7)
    # Keep integer identity through glare gaps, then exclude contaminated measurements.
    # Include the sigma0.6 Gaussian's 3px support and remap's bilinear footprint.
    saturated = cv2.dilate((gray == 255).astype(np.float32), np.ones((7, 7), np.uint8))
    keep = np.ones(len(corners), bool)
    if saturated.any():
        for n, quad in enumerate(windows):
            for *_, samples in edge_samples(quad):
                if cv2.remap(saturated, samples[:, :, 0].astype(np.float32),
                             samples[:, :, 1].astype(np.float32), cv2.INTER_LINEAR).any():
                    keep[n] = False
                    break
    if diagnostics is not None:
        diagnostics.update(squares_before_saturation_filter=len(corners),
                           saturated_edge_squares_removed=int((~keep).sum()))
    if keep.sum() < 12:
        raise ValueError("Need at least 12 square centres with unsaturated edge windows")
    return np.asarray(corners)[keep], indices[keep]


def observations(folder, size, pitch_m):
    files = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in {".png", ".bmp", ".tif", ".tiff"})
    if not files:
        raise ValueError(f"No PNG/TIFF/BMP images in {folder}")
    views = []
    for path in files:
        gray = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if gray is None or gray.dtype != np.uint8 or gray.ndim != 2 or gray.shape[::-1] != size:
            raise ValueError(f"{path}: expected original Mono8 {size}; do not resize")
        try:
            diagnostics = {}
            corners, indices = detect_lattice(gray, diagnostics=diagnostics)
        except ValueError as exc:
            print(f"Rejected {path.name}: {exc}")
            continue
        centres = square_centres(corners)
        objects = np.column_stack((indices * (np.sqrt(2) * pitch_m), np.zeros(len(indices))))
        views.append((path, objects.astype(np.float32), centres[:, None].astype(np.float32)))
        coverage = image_coverage(centres, size)
        print(f"Using {path.name}: {len(centres)} indexed square centres; "
              f"8x8 coverage {coverage['occupied_cells']}/64 ({coverage['fraction']:.1%}); "
              f"saturated edge squares removed: {diagnostics['saturated_edge_squares_removed']}")
        if coverage["empty_corners"]:
            print(f"Review {path.name}: empty image corner bins: {', '.join(coverage['empty_corners'])}")
    return views


def image_coverage(pixels, size):
    cells = np.floor(np.asarray(pixels).reshape(-1, 2) / size * 8).astype(int)
    occupied = {tuple(cell) for cell in cells if np.all((cell >= 0) & (cell < 8))}
    corners = {"top_left": (0, 0), "top_right": (7, 0),
               "bottom_left": (0, 7), "bottom_right": (7, 7)}
    return {"cols": 8, "rows": 8, "occupied_cells": len(occupied),
            "fraction": len(occupied) / 64,
            "empty_corners": [name for name, cell in corners.items() if cell not in occupied]}


def residual_map(pixels, residuals, size):
    result = []
    cells = np.floor(pixels / size * [12, 10]).astype(int)
    for row in range(10):
        for col in range(12):
            selected = residuals[np.all(cells == [col, row], axis=1)]
            result.append({"col": col, "row": row, "count": len(selected),
                           "mean_dx_px": float(selected[:, 0].mean()) if len(selected) else None,
                           "mean_dy_px": float(selected[:, 1].mean()) if len(selected) else None,
                           "rms_px": float(np.sqrt(np.mean(np.sum(selected ** 2, axis=1)))) if len(selected) else None})
    return {"cols": 12, "rows": 10, "residual_convention": "observed minus projected", "cells": result}


def fit_calibration(views, checks, size, serial, pitch_m):
    if len(views) < 12 or len(size) != 2 or min(size) <= 0:
        raise ValueError("Need at least 12 usable training views and positive image dimensions")
    if not serial.strip() or not np.isfinite(pitch_m) or pitch_m <= 0:
        raise ValueError("Specify a camera serial and positive finite cell pitch")
    training = {p.resolve() for p, _, _ in views}
    if any(p.resolve() in training for p, _, _ in checks):
        raise ValueError("Training and check images must be separate")
    for path, objects, pixels in views + checks:
        if (len(objects) < 12 or np.asarray(objects).shape != (len(objects), 3)
                or np.asarray(pixels).shape != (len(objects), 1, 2)
                or not np.isfinite(objects).all() or not np.isfinite(pixels).all()
                or np.any(np.asarray(objects)[:, 2] != 0)
                or np.linalg.matrix_rank(np.asarray(objects)[:, :2] - np.mean(objects, axis=0)[:2]) < 2):
            raise ValueError(f"{path}: invalid planar calibration observations")
    models = []
    for flags in (0, cv2.CALIB_FIX_K3 | cv2.CALIB_ZERO_TANGENT_DIST):
        rms, matrix, distortion, rotations, translations = cv2.calibrateCamera(
            [v[1] for v in views], [v[2] for v in views], size, None, None, flags=flags,
            criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 100, 1e-9))
        model = {"camera_provider": "basler", "camera_serial": serial.strip(),
                 "width": size[0], "height": size[1], "model": "opencv",
                 "fx": float(matrix[0, 0]), "fy": float(matrix[1, 1]),
                 "ppx": float(matrix[0, 2]), "ppy": float(matrix[1, 2]),
                 "coeffs": distortion.ravel().tolist()}
        opencv_calibration(model, size)
        if not (np.isfinite(rms) and 0 <= model["ppx"] < size[0] and 0 <= model["ppy"] < size[1]):
            raise ValueError("Invalid fit: collect sharper, varied views")
        per_view, fresh, all_residuals, all_pixels, check_residuals, check_pixels = [], [], [], [], [], []
        for n, (path, objects, pixels) in enumerate(views + checks):
            if n < len(views):
                rotation, translation = rotations[n], translations[n]
            else:
                ok, rotation, translation = cv2.solvePnP(objects, pixels, matrix, distortion)
                if not ok:
                    raise ValueError(f"Could not estimate check pose: {path}")
            projected, _ = cv2.projectPoints(objects, rotation, translation, matrix, distortion)
            residuals = (pixels - projected).reshape(-1, 2)
            item = {"image": str(path.resolve()), "points": len(objects),
                    "image_coverage": image_coverage(pixels, size),
                    "reprojection_rmse_px": float(np.sqrt(np.mean(np.sum(residuals ** 2, axis=1))))}
            (per_view if n < len(views) else fresh).append(item)
            (all_residuals if n < len(views) else check_residuals).append(residuals)
            (all_pixels if n < len(views) else check_pixels).append(pixels.reshape(-1, 2))
        pixels = np.concatenate(all_pixels)
        normals = np.array([cv2.Rodrigues(r)[0][:, 2] for r in rotations])
        tilt = float(np.degrees(np.arccos(np.clip(normals @ normals.T, -1, 1))).max())
        coverage = float(cv2.contourArea(cv2.convexHull(pixels)) / np.prod(size))
        dataset_coverage = {"training": image_coverage(pixels, size),
                            "fresh_checks": image_coverage(np.concatenate(check_pixels) if checks else [], size)}
        warnings = []
        for key, items in (("training", per_view), ("fresh_checks", fresh)):
            if not items:
                continue
            empty = dataset_coverage[key]["empty_corners"]
            if empty:
                warnings.append(f"{key} dataset has empty image corner bins: {', '.join(empty)}")
            sparse = sum(bool(item["image_coverage"]["empty_corners"]) for item in items)
            if sparse:
                warnings.append(f"Empty image corner bins in {sparse}/{len(items)} {key} views; inspect per-view image_coverage")
        if tilt < 20:
            warnings.append("Limited tilt diversity: include +/-10-15 deg about both axes")
        if coverage < .4:
            warnings.append("Limited image coverage: include all edges and corners")
        if rms > .1 or any(c["reprojection_rmse_px"] > .1 for c in fresh):
            warnings.append("RMS exceeds 0.1 px; inspect square edges, focus, lattice and flatness")
        if not checks:
            warnings.append("No held-out views supplied; fresh image checks are missing")
        model["estimation"] = {
            "validated": False, "method": "encoder_plain_square_centres", "flags": flags,
            "nominal_cell_pitch_m": pitch_m, "plain_square_lattice_pitch_m": float(np.sqrt(2) * pitch_m),
            "reprojection_rmse_px": float(rms), "per_view": per_view, "fresh_checks": fresh,
            "image_coverage_fraction": coverage, "board_normal_span_deg": tilt,
            "dataset_coverage": dataset_coverage,
            "residual_map": residual_map(pixels, np.concatenate(all_residuals), size),
            "check_residual_map": residual_map(np.concatenate(check_pixels), np.concatenate(check_residuals), size) if checks else None,
            "warnings": warnings,
            "note": "Software estimate. Reprojection does not establish physical accuracy or plate flatness. "
                    "Uniform pitch error rescales translations; anisotropic and local errors bias intrinsics.",
        }
        models.append(model)
    full, restricted = models
    full["estimation"]["comparison"] = restricted
    if max(abs(full[k] - restricted[k]) / max(abs(full[k]), 1) for k in ("fx", "fy", "ppx", "ppy")) > .002:
        full["estimation"]["warnings"].append("Full and restricted intrinsics differ by over 0.2%; review both models")
    return full


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--check", type=Path, help="Held-out images required for fitting")
    parser.add_argument("--pitch-mm", required=True, type=float, help="Nominal adjacent cell pitch, not square width")
    parser.add_argument("--serial", required=True, help="Actual camera serial number")
    parser.add_argument("--width", type=int, default=2448)
    parser.add_argument("--height", type=int, default=2048)
    parser.add_argument("--output", type=Path, help="New intrinsics JSON; never overwrites")
    parser.add_argument("--detect-only", action="store_true")
    args = parser.parse_args()
    size, pitch = (args.width, args.height), args.pitch_mm / 1000
    if min(size) <= 0 or not np.isfinite(pitch) or pitch <= 0 or not args.serial.strip():
        parser.error("Specify positive dimensions, finite positive pitch and a serial")
    if not args.detect_only and (not args.output or not args.check):
        parser.error("Fitting requires --output and a separate --check folder")
    if args.output and args.output.exists():
        raise ValueError(f"{args.output} exists; choose a new output filename")
    views = observations(args.images, size, pitch)
    if args.detect_only:
        print(f"Accepted {len(views)} indexed views (detection only; no intrinsics fitted)")
        return 0 if views else 1
    checks = observations(args.check, size, pitch)
    if len(checks) < 5:
        raise ValueError("Need at least five usable held-out views")
    result = fit_calibration(views, checks, size, args.serial, pitch)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    for name, model in (("Full", result), ("Restricted", result["estimation"]["comparison"])):
        print(f"{name}: fx={model['fx']:.3f} fy={model['fy']:.3f} "
              f"cx={model['ppx']:.3f} cy={model['ppy']:.3f} "
              f"RMS={model['estimation']['reprojection_rmse_px']:.5f} px")
    for warning in result["estimation"]["warnings"]:
        print(f"Review: {warning}")
    print(f"Wrote {args.output} (estimate; requires review)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, cv2.error) as exc:
        sys.exit(f"Error: {exc}")
