# Robot steps, Basler preparation and shorter UI

> Historical record for source `44f2f0aa` (10–11 November 2026). It does not
> describe the current review-fix revision; see the package-level `RELEASE_NOTES.md`.

The reviewed release is installed. Refresh the laptop module page. The IPC owns
hardware work. The laptop supplies the browser UI. All three modules still run
without the ano app.

## Use Robot steps

1. Open Robot steps in the main panel of Calibration, Teaching or Pick/place.
   Choose an XYZ step or a tool rotation size.
2. Select one positive or negative axis. This prepares an endpoint. It does not
   move the arm. Review the exact start, target and settings in Current operation.
3. Check the approval box and select Approve endpoint / move. The IPC checks the
   frozen joint path before motion. Use Stop operation in the same card to stop.

XYZ increments use robot-base axes. Rotations use the active TCP's local axes.
The defaults are 5 mm, 1 degree and 3 degrees per second. Robot step settings
contains the address, TCP, scene, workspace and operating joint bounds.
Saved settings are a draft. They do not confirm the physical setup.

The unblended joint path can curve and change TCP height or orientation. The
workspace bounds apply to the start and endpoint. The scene checks the whole
joint path. The current supervised-self scene does not cover external objects.
Use the existing checked vertical-descent path when a straight descent is needed.

Refresh and inspection do not send motion or approve a request. An unknown
network outcome requires inspection. Stop ends the step without resume.
The robot card does not send gripper commands or require a camera/project.

## Basler path

The selected preparation is a wrist mounted a2A2448-23gmBAS GigE camera with a
12 mm lens. Calibration has an explicit camera selector. RealSense remains the
default. Provider, image profile and exposure flow into Teaching and Pick/place.

Pypylon 4.2.0 is installed in the IPC virtual environment. No SDK import, device
enumeration or physical Basler test was performed. Before capture, supply the
actual serial and measured lens intrinsics for the exact image profile. The
12 mm lens specification and D435 intrinsics cannot supply that calibration.

The 35 original fine-board files are installed under
`data/boards/stage2_charuco_basler_32mm`. Select that manifest for tracking the
32 mm fine boards. The hand-eye calibration board is a separate selection.
No current 80 mm board or saved project was replaced.
See the installed BASLER_PREPARATION.md for the first lens-calibration procedure.

The saved D435 intrinsic estimate used 16 views and reports 0.177 px fit RMSE.
It remains marked unvalidated, with 3.83 percent image coverage. Factory values
were also retained. See results/intrinsics-provenance.json.

## Release and evidence

Source: `44f2f0aa68b8b7afcd18ef8c07abd5f94f3607079d65863fb4510d19e28a44e4`.
IPC source:
`/home/user/anodizing_line/anodizing_modules_20261104/bundle-robot-control-44f2f0aa-20261110/anodizing-modules`.
PID 1066330 and start ticks 35715779 are point-in-time identifiers.

The 397 source hashes, three rendered pages and eleven served assets matched.
All 862 existing data files retain their hashes. Exactly 35 reviewed board files
were appended. Cached ownership was idle and clean. Power information was cached.

Evidence includes 36 focused core/browser checks, 78 existing owner/motion/MCU
checks, and 55 native checks plus two corrected stale CLI cases. Seventeen native
cases skipped. Their individual reasons were not listed by pytest. All device
work in these checks used fakes. Two screenshots record the UI review.

ROOT_REVIEW.md and activation/activation.json contain the backup, source, SDK
and readback hashes. Previous bundles and data backups remain available.
No physical robot motion, MCU operation, voltage change or camera capture was tested.
