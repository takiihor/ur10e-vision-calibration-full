# Robot steps, Basler preparation and UI text

> Historical record for source `44f2f0aa` (10–11 November 2026). It does not
> describe the current review-fix revision; see the package-level `RELEASE_NOTES.md`.

The user selected finite XYZ steps and tool rotation. The user also requested
Basler preparation and shorter UI text. The confirmed Basler setup is a wrist
mounted a2A2448-23gmBAS GigE camera with a 12 mm lens and 32 mm fine-stage boards.
The existing RealSense selection remains the default.

Final source: `44f2f0aa68b8b7afcd18ef8c07abd5f94f3607079d65863fb4510d19e28a44e4`.
It contains 397 files. The archive SHA-256 is
`05161805b0d817561b365f84055429ec76e4a991f1f67f1d31cfd6e1d0a1e0a1`.
The coordinator independently checked each file and archive entry.

## Reviewed behavior

Robot steps use base XYZ axes and local tool rotation axes. Matrix composition
defines rotation about the current TCP. The requested position is an endpoint.
The unblended joint path can curve and change TCP height or orientation.
Workspace limits apply to the start and destination. The scene checks the arm
and attached geometry along the joint path. This does not add missing cell
objects to the existing supervised-self scene.

One click prepares an endpoint. The existing Current operation card holds its
exact approval and Stop. After approval, the IPC checks the frozen joint path
before dispatch. The worker reuses the existing JobOwner, native SDK worker,
model/scene binding, active TCP check, evidence and confirmed stop handling.
It does not open a camera or depend on a teaching project. It sends no gripper
command. Refresh and inspection send no motion and grant no approval.

The Basler adapter moves to the shared camera package. The previous pick/place
import retains its vision-error contract. Calibration adds explicit provider
selection and requires serial-bound measured lens intrinsics before hardware
access. The bridge records the profile and exposure for Teaching and Pick/place.
It does not calculate camera intrinsics from the nominal 12 mm focal length.
See the candidate's BASLER_PREPARATION.md for the first lens-calibration step.

The text pass shortens action instructions and moves repeated explanations into
closed details. It preserves motion contracts, required approval conditions,
physical grip confirmation and the limits of MCU acknowledgements. It is not
a claim of compliance with the ASD controlled dictionary.

## Review findings addressed before acceptance

- A failed robot request lost its visible error. A successful request also let
  the buttons become available before ownership status updated.
- A definitive validation rejection must permit an explicit correction. An
  unknown network outcome must retain inspection without automatic retry.
- An explicit camera-provider mismatch must fail in either direction.
- Basler exposure and frame-rate metadata must distinguish requested settings
  from SDK readback. Frame timestamps use the host monotonic clock.
- Expanded HTML must keep valid containers and single IDs after fragment
  insertion. Approval remains in the right-hand operation card.

Terra recorded the failing tests before the fixes. Thirty-six focused core and
browser checks passed on source `13115877`. Later changes remove a duplicate
test file, correct two stale upstream CLI fixtures and format the displayed
joint speed as 3.00 degrees per second. No motion backend changed. The fixture
corrections retain both zero-motion assertions. The two corrected cases passed.
A focused fake-browser check verified the speed format. The coordinator checked
each manifest delta and archive. No passing backend suite was repeated.

The laptop's pinned environment passed 78 existing owner, manual, repeat and
MCU checks, including the offline guard. Initial attempts on the ThinkCentre
exposed missing Pillow and incompatible local OpenCV dependencies. Those errors
were not counted as product passes. The native hand-eye checks passed 55 cases
and skipped 17 cases. The two stale fixtures then passed in a focused run.
The pytest output did not list individual skip reasons. The suite contains
optional desktop and import checks. Browser runtime checks ran separately.

The release includes 35 unchanged Basler board assets. Each matches the user's
original two_stage.zip. The hand-eye calibration board and the tracking boards
remain separate explicit selections. No saved project is retagged or migrated.

## Installation

The standing installation authorization applies. The IPC runs this source in
`bundle-robot-control-44f2f0aa-20261110/anodizing-modules` under
`/home/user/anodizing_line/anodizing_modules_20261104`.
PID 1066330 and start ticks 35715779 identify the verified process at this time.
Future operations must read its current identity. The previous process exited
with SIGINT. The interpreter, normal kernel, data directory, model and service
flags remain the same. The previous bundle remains available.

All 397 source files, three rendered pages and eleven served assets matched.
The cached owner was idle, without pending MCU work, storage error, closing,
uncertain execution or recovery restriction. Robot steps were enabled and
available. MCU capability was available. Power information was cached only.

The fresh backup covers 862 existing files. All hashes remained unchanged.
Exactly 35 reviewed Basler board files were added. Existing projects, jobs,
calibrations, models and scene selections were not overwritten.
Backup SHA-256:
`03b9550250cfa9b50a6f2bfc7789a285c25efd89ec08d78c41346df520335ae6`.
Inventory SHA-256:
`9432042ee705d494ca5ed94a705895cad7c7dc7df3734264d02bbf2efa832cb9`.

IPC DNS prevented direct package installation. The official pypylon 4.2.0 wheel
was transferred through the laptop. It was installed in the IPC venv with
no index access and no dependency changes. Its SHA-256 is
`d66b7367e1ddd0b312b785bbc9688de5a2d5f4f083e095084bf5a4a978729d8f`.
Only package metadata was inspected. No SDK import or device enumeration ran.
See activation/activation.json and activation/pypylon-evidence.json.

## Existing D435 intrinsic evidence

The IPC calibration dated 6 November uses an estimated intrinsic override from
16 ChArUco views. The reported intrinsic fit RMSE is 0.177 px. The saved file
marks validation false and reports only 3.83 percent image coverage. The hand-eye
reprojection RMSE is 0.407 px. Factory intrinsics remain separately recorded.
These values belong to D435I serial 213622073560 at 1280 by 720. They do not
calibrate the Basler camera. See results/intrinsics-provenance.json.

## Physical limits

No physical robot movement, MCU command, voltage change or camera capture is
part of this implementation review. Basler serial, lens intrinsics, GigE image
quality and physical hand-eye verification remain required before Basler use.
The optional SDK installation and software tests cannot establish these facts.
