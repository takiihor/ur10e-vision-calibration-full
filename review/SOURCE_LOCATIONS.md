# Source locations and review guide

This file is a historical record of the 10 November 2026 review. The full
unpacked source was retained on both PCs at that time, and both copies passed the
397-file SHA-256 manifest check.
Historical release: `44f2f0aa68b8b7afcd18ef8c07abd5f94f3607079d65863fb4510d19e28a44e4`.
For the current review-fix revision, use the package-level
`SOURCE_MANIFEST.sha256`, `CONTENTS.sha256` and `provenance/` files.

## This ThinkCentre

Source:
`/home/nep/anodizing_line/UR10e/interface_pc/review_work/robot_control_20261110/work/anodizing-modules`

Review records:
`/home/nep/anodizing_line/UR10e/interface_pc/review_work/robot_control_20261110`

## IPC

Source:
`/home/user/anodizing_line/anodizing_modules_20261104/bundle-robot-control-44f2f0aa-20261110/anodizing-modules`

Review records:
`/home/user/anodizing_line/review/robot_control_20261110`

Start file:
`/home/user/anodizing_line/REVIEW_SOURCE.md`

The IPC source is also the installed runtime source. Make a separate working
copy before editing it. Runtime projects, jobs and calibration data are in
`/home/user/anodizing_line/anodizing_modules_20261104/data`.

## What to read

- `app/anodizing_web/`: shared web service, browser screens and hardware-job coordination.
- `app/test/`: module and browser checks.
- `vendor/ur10e-handeye/`: native calibration module.
- `vendor/ur-pick-place/`: native teaching and pick/place module.
- `vendor/charuco-realsense/`: shared camera and board support.
- `vendor/charuco-generator/`: board generator support.
- `README.md` and `BASLER_PREPARATION.md` inside the source: setup and camera instructions.

In the review records, read HANDOFF.md, ROOT_REVIEW.md and STE_COPY.md first.
MANIFEST.sha256 is the historical complete source manifest for that release.
SOURCE_ID identifies that historical manifest. CHANGES.patch and
CHANGED_FILES.json describe the historical review delta. They do not describe the
current review-fix revision; use the package-level manifests for that.

From the ThinkCentre source directory, check all source files with:

```sh
sha256sum -c ../../MANIFEST.sha256
```

On the IPC, the manifest is one level above its source directory, so use:

```sh
sha256sum -c ../MANIFEST.sha256
```

This retention check reads files only. It does not restart services, capture
images, send MCU commands or move the robot. Hardware validation remains as
described in HANDOFF.md.
