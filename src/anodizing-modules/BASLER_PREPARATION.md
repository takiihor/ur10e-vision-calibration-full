# Basler camera preparation

The planned wrist camera is the Basler a2A2448-23gmBAS GigE monochrome camera with a **12 mm lens**. The fine-stage boards remain 32 mm. This change does not replace any board, project or calibration.

The IPC owns the camera SDK and capture process. The laptop opens the standalone browser UI. Calibration, Teaching and Pick/place remain independent of the ano app.

## Prepared software path

- Calibration has an explicit RealSense/Basler selector. RealSense remains the default.
- Basler preview and capture require an explicit camera serial and measured lens intrinsics before hardware access.
- Capture uses the existing Basler software-trigger adapter. The shared adapter now resides in `charuco_realsense.basler`. The previous pick/place import preserves its `VisionError` contract.
- Capture records provider, serial, image profile, requested FPS, FPS readback, exposure readback and gain readback. Frames contain BGR pixels converted from native Mono8, frame numbers and host monotonic timestamps. Host timestamps do not establish robot/camera synchronization accuracy.
- Teaching retains the provider from the calibration handoff. Teaching and manual Pick/place retain the recorded profile and exposure. A recorded provider mismatch stops setup.

The draft full-frame profile is **2448 × 2048, 20 FPS, Mono8, 5000 µs exposure, 0 dB gain**. The calibration page exposes **Exposure (µs)** and **Gain (dB)** when Basler is selected. **Preview camera (no pose)** uses these values and works without an intrinsics file; it only checks image quality and marker visibility. The CLI accepts `--exposure-time-us` and `--gain` for the same values. The selected intrinsic file supplies image dimensions. Capture resets ROI offsets, binning and decimation. The adapter checks image size readback and frame dimensions. FPS readback does not guarantee delivered FPS. Exposure and gain readback are recorded in the camera metadata and reused by Teaching and Pick/place through the saved calibration.

## Required first physical calibration

No Basler camera or serial was available during preparation. No Basler lens intrinsic file exists in this candidate. The D435 fitted intrinsics belong to the D435 and cannot calibrate this camera.

1. Mount the final 12 mm lens rigidly. Set focus and aperture for the intended working distance.
2. Use pylon Viewer or an external OpenCV calibration procedure to acquire varied board images. Use the intended image profile, ROI and lens settings.
3. Measure the printed board. Fit the camera matrix and distortion from views distributed across the image. Validate with fresh views.
4. Save the measured model as JSON. Include `camera_serial`, `camera_provider: "basler"`, `width`, `height`, `fx`, `fy`, `ppx`, `ppy`, `model: "opencv"` and `coeffs`. Use the actual serial. Do not insert a nominal focal length as pixel intrinsics.
5. Select that JSON and serial in Calibration. Capture a stationary board view. Collect a new hand-eye session and separate verification views.
6. Teach a new project with the resulting Basler calibration. Review the 32 mm board, TCP and physical setup before operation.

Changing focus, aperture, image geometry, lens or mount requires review and usually new intrinsic and hand-eye calibration. No fabricated camera matrix or automatic D435 profile migration is provided.

## IPC dependency and CLI

`pypylon` remains optional and imports only during Basler capture. The existing constraint remains `pypylon>=4,<5`. The shared camera and hand-eye packages now expose the same `basler` extra. Install only in the reviewed IPC virtual environment. Preparation did not install or upgrade packages.

Example native camera-only check, with paths and serial replaced by reviewed local values:

```sh
ur-handeye check --camera-provider basler --serial ACTUAL_SERIAL \
  --intrinsics /ipc/data/calibrations/basler/lens-intrinsics.json \
  --manifest /ipc/data/boards/calibration_stage2/board_manifest.json \
  --board calibration_stage2 --fps 20 --exposure-time-us 5000 --gain 0
```

The command name `check` permits camera-only operation when `--robot-ip` is omitted. See the package CLI help for current switches. The browser service still needs its camera opt-in. Manual robot capture still needs its separate manual opt-in.

## Evidence and limits

Terra recorded failing fake-hardware tests before implementation in `../../results/basler-baseline-e810-red.txt` and `../../results/basler-teaching-e810-red.txt`. These tests cover missing own intrinsics, provider mismatch and lost exposure. Further RED evidence is `../../results/basler-provider-red.txt` and `../../results/basler-readback-red.txt`. Final test results belong to the candidate review record.

Preparation does not establish GigE connectivity, exposure quality, board coverage, lens accuracy, hand-eye accuracy, collision clearance or physical Pick/place performance. It does not command a camera or robot. No SDK installation, deployment or hardware validation occurred.

Official references: [Basler model](https://docs.baslerweb.com/a2a2448-23gmbas), [Python programming guide](https://docs.baslerweb.com/python-prog-guide), [pypylon](https://github.com/basler/pypylon).

## Board selection and bundled provenance

For Basler Teaching/tracking, explicitly select `data/boards/stage2_charuco_basler_32mm/board_manifest.json` and the correct tank board. For hand-eye capture, use the separate `data/boards/calibration_stage2/board_manifest.json` and `calibration_stage2` board. The existing D435 80 mm board selection stays unchanged. No automatic board swap occurs.

The bundled 35 fine-board files are exact copies from the user-provided `two_stage.zip`, SHA-256 `58c94b96a6ef6967efc4127fb59bbfa01ccccf3d0e37b71d5377a4acfaf5f226`. Every copied file matches its original archive entry. `BASLER_BOARD_PROVENANCE.json` in the release review directory records individual hashes. No board dimensions, marker IDs or generated assets changed. These are immutable board assets, not IPC runtime profiles, jobs or captures.

The existing `vision-basler` example uses a 30 mm ArUco demonstration target. It does not select the requested 32 mm fine ChArUco boards. Select the explicit fine-board manifest when preparing the new Basler teaching project.
