# Basler encoder-plate intrinsics

The two standalone commands use the Vision Encoder plate's plain bright and dark squares.
They do not decode proprietary marks or change the existing ChArUco workflow.
Use the existing NumPy/OpenCV/pypylon environment; no new dependencies are needed.

Capture only on the camera PC. Hold a flexible steel plate flat against a
magnetic board with full contact; a rigid flat rig is the next improvement.
Lock focus and aperture before the first capture, choose the final working
distance, and stay inside the depth of field. Avoid glare on measured square
edges. Long exposures can be suitable when the camera and plate remain still;
use shorter exposures for motion or a responsive preview. This capture command
currently accepts exposures up to 20000 microseconds (20 ms), so provide enough
illumination within that software limit. The limit does not establish a
physical calibration requirement.

Both commands require `--serial`; neither selects a camera by default. Set
`BASLER_SERIAL` to your camera's actual numeric serial before using these examples.

```sh
python tools/capture_encoder_intrinsics.py --serial "$BASLER_SERIAL" \
  --capture-dir /path/to/fresh/session --split train --count 25 \
  --pitch-mm 1.68 --exposure-us 10000 --gain-db 0 --fps 5
python tools/capture_encoder_intrinsics.py --serial "$BASLER_SERIAL" \
  --capture-dir /path/to/fresh/session --split check --count 5 \
  --pitch-mm 1.68 --exposure-us 10000 --gain-db 0 --fps 5
```

**1.68 mm is an approximate nominal adjacent-cell pitch, not a published or
calibrated dimension.** Substitute your nominal pitch consistently. Plain-square
neighbours are diagonally separated cells; the software uses `sqrt(2)*pitch` for
their lattice spacing. A uniform pitch error rescales translation; nonuniform
geometry and bending can bias intrinsics.

Include flat views, ±10–15° tilts about both axes, in-plane rotations spanning
0–90°, and points near every image edge/corner. The whole plate need not be in
view. Space freezes a valid indexed frame, Enter saves, R retakes, Esc quits.
Saved PNGs retain original 2448 × 2048 Mono8 pixels. Profiles must match between
train/check; existing split folders are refused and incomplete sessions cannot
resume. Pose diversity, sharpness and physical flatness require operator checks.

Fit later without hardware:

```sh
python tools/encoder_intrinsics.py --images /path/to/fresh/session/train \
  --check /path/to/fresh/session/check --serial "$BASLER_SERIAL" --pitch-mm 1.68 \
  --output /path/to/fresh/session/intrinsics-estimate.json
```

The CLI requires at least 12 accepted training views and five accepted fresh
checks. Output creation never overwrites. Use `--detect-only --images FOLDER
--serial "$BASLER_SERIAL" --pitch-mm 1.68` for detection/indexing without fitting.
Inputs must be native
Mono8 PNG/TIFF/BMP, without resizing, ROI or binning changes. Keep copied images
and repeated training poses out of the check set.

The top-level JSON is the full OpenCV five-coefficient model, compatible with
`ur_handeye.intrinsics.load_intrinsics`. `estimation.comparison` contains a second
fit with k3 and tangential distortion fixed to zero. Both report per-view RMS,
held-out pose/reprojection checks, coverage/tilt and 12 × 10 residual maps for
training and check images. Maps contain count, mean observed-minus-projected
x/y residual, and RMS per bin; empty bins are null. Held-out images do not fit K.
Each `per_view`/`fresh_checks` item includes `image_coverage`: the number and
fraction of occupied bins in an 8 × 8 image grid, plus `empty_corners` (the four
corner bins). `estimation.dataset_coverage.training` and `.fresh_checks` report
the union of occupied bins in each set; an absent check set has zero occupancy.
Empty corner bins produce warnings for each dataset and for sparse individual
views. The existing `image_coverage_fraction` remains the training convex-hull
area fraction; it can hide internal holes, so inspect grid coverage too.
Review RMS above 0.1 px, model disagreement above 0.2%, map structure and missing
edge coverage. No output is automatically installed into module configuration.

Candidate detection normalizes local illumination/contrast before thresholding
both the original grayscale and its inverse. Each polarity has its own size
filter because the dark squares can be larger. A shared lower size bound also
rejects small code-ring interiors: plain-square widths must differ by no more
than 2.5 times. An interior brightness/uniformity
check rejects outlined or textured coded cells. It checks the central 21 × 21
pixels of each normalized 33 × 33 candidate; a smaller patch can miss texture
around a flat code centre.
These candidates seed the largest connected lattice component; they are not the
final measurement set. Within that component, a seed candidate needs at least
twelve candidates including itself within three lattice spacings. The nearest
such dense candidate to the image centre anchors a compact seed: all candidates
in that component within six spacings are considered for the initial basis. Keep
only the anchor-connected spatial patch after clipping, using the same four-spacing
neighbour support. Clipping can break a path that ran outside the seed radius;
disconnected candidates remain eligible for normal prediction/refinement later.
This avoids sparse
dark contours at the image centre and distant white islands separated by large
legitimate gaps. All original seed cycle, phase and geometry checks still apply;
there is no retry with a different phase after a failed check.
`detect_lattice` grows outward at fixed integer plain-cell
positions. A homography through the eight nearest accepted cells predicts each
new outline locally, so radial distortion is followed rather than extrapolating
one global homography. Four outline sizes span the bright/dark width difference.
Size bounds use the local homography scale so perspective at the image edge does not inherit the
seed region's scale.
Native edge searches follow one gradient peak along each side, reject clipped
search peaks, and require four straight lines, plausible size/shape, contrast
and uniform interior. Connected dark background seams therefore do not require
isolated contours. Code cells have half-integer positions in this lattice and
are never proposed. A measured centre must agree with its prediction within
`max(0.5 px, 2% of seed spacing)`; its integer identity is never changed.
Measurements use native-resolution grayscale edges (with light noise smoothing),
four fitted lines and diagonal
intersections. Only these centres enter calibration; fitted corners are used
to construct centres and draw overlays. Both polarities share one lattice.
The detector records the actual successful edge-search windows for seeds and
grown squares. After indexing, it removes any square whose window contains a
native Mono8 value of 255, including the 3 px smoothing support and bilinear
sampling footprint. This uses the original image, so an inverted dark pixel or
a contrast-normalized maximum does not count as saturation. A saturated centre
outside those windows does not remove a square. Removed measurements leave
legitimate gaps without changing the surviving integer identities. Saturated
measurements may support local prediction/indexing but never enter calibration.
The CLI prints the number removed per frame; `detect_lattice(gray, diagnostics)`
also reports `squares_before_saturation_filter` and
`saturated_edge_squares_removed`. Counts refer to successfully measured squares,
not failed trial windows or every physical cell hidden by glare. At least twelve
unsaturated measurements must remain. Strong glare can still prevent seeding,
edge measurement or consistent indexing before this filter runs.
Seed indexing predicts integer neighbour offsets, including links across missing cells
of up to four lattice steps, then checks reciprocal links, cycles, unique indices
and local homographies. Growth tries positions up to two lattice steps away, including
one-cell gaps, and checks unique integer identities and local homographies after measurement.
The seed check uses 12 nearby points; the grown field uses eight to keep the check local
under radial curvature. Ambiguous or inconsistent gaps are rejected. The local
geometry tolerance is `max(0.5 px, 3% of lattice spacing)`; this is an indexing
check and permits defocused centre uncertainty. It does not establish subpixel
measurement precision or make blurred frames suitable for calibration.

The committed untouched Mono8 crops and independent manual cell labels in
`tools/tests/fixtures/` guard real-frame detection/indexing. The three old
diagnostic frames must be used for detection only: they are a single defocused
view. Sharp new operator captures require separate evidence. Severe glare,
extreme blur/perspective or wider gaps can still reject views. Full-field growth
is intended for offline analysis and can take seconds per native frame; the
capture preview also uses this path and may update slowly. Software evidence
does not establish actual plate geometry or physical camera accuracy. Separate
calibrations are needed after changing locked optics.
Captures made while changing focus are invalid for intrinsics and may be used
only for detector development. A provisional evaluation of a flexible plate
showed that full contact against a magnetic board improved the fit compared
with a loose plate. Residual radial structure, bending and noisier dark-square
measurements still require review; a rigid flat target is preferable.
Operator acceptance of a provisional fit does not establish metric or physical
accuracy. Any focus/aperture change or lens removal invalidates that calibration.
Keep measured intrinsics and deployment acceptance records outside this tool's
source tree; no reusable calibration data is supplied here.

Offline checks (standard-library unittest; no camera, GUI or robot access):

```sh
python -m unittest discover -s tools/tests -v
```
