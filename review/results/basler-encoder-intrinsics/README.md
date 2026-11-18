# Provisional Basler encoder-plate intrinsics

[intrinsics.json](intrinsics.json) records the restricted OpenCV model (`k1`,
`k2`) accepted provisionally on 17 November 2026 for Basler serial `42007093`
at native 2448 × 2048 with the same ROI and binning. Holdout RMS was **0.340 px**
over five views. The radial residual rose from roughly **0.22 px at the centre to
0.6 px near the corners**. Leave-one-out standard deviations were about **1.06 px
for fx** and **0.2 px for cx and cy**. The fitted fx was within **0.7%** of nominal.

The plate was held on a magnetic board. Plate bending (H1) is the leading
explanation for residual structure and reduced holdout RMS by 41% in the fitted
model. Plate flatness was unmeasured, and a rigid flat rig would reduce this
source of error. The uniform **1.68 mm** cell pitch is an unmeasured nominal
estimate. Uniform scale leaves the intrinsics unchanged. For metric hand-eye,
measure the pitch or fit the scale.

The capture frames did not record the camera serial. The camera identity comes
from it being the only Basler attached during capture. All 25 frames, including
the five holdout views, are byte-distinct (checked by SHA-256).

The estimate applies only to this lens at approximately **30 cm focus and F8**
with both settings locked. Any focus or aperture change or lens removal
invalidates the estimate. Status is **provisional and unvalidated**. Verification
was software-only and covered neither a metric robot check nor tests on the IPC,
robot or AGV. The files are review artifacts. The launcher installs only the
bundled boards and leaves calibration selection to the operator.

[The acceptance record](acceptance_validation.json) records original and published
hashes. Both exports retain frame names as references to the capture archive.
[The rational fit](intrinsics_rational_comparison.json) is included for comparison.
