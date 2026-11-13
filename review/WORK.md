# Finite robot control card — 2026-11-10

> Historical record for the `44f2f0aa` review. It does not describe the current
> review-fix revision; see the package-level `RELEASE_NOTES.md`.

Base: immutable operation-controls source `e8109e8a95787c2691f4eccf5ca698d94b59677c520060ab88bbc44247a21cc6`. Working copy: `work/anodizing-modules`. Previous releases remain unchanged.

Confirmed user scope: finite endpoint steps in base X/Y/Z and finite rotation about active tool/TCP local axes. Existing checked joint trajectories can curve and change height. No continuous jog, held-repeat, power or brake controls. Proposed fixed steps: 1/5/10 mm (default 5); rotations 1/2/5 degrees (default 1); reviewed joint speed defaults 3 degrees/s. One exact-plan approval and existing Stop/status use Current operation; initiating card stays main on all three standalone pages.

Use shared JobOwner and the existing native checked joint worker. No calibration/teaching/camera dependency, new controller or dependencies. Validate and pin robot host/TCP, scene/model, workspace, operating joint bounds/source and speed. Tool rotations multiply current orientation by the local-axis increment and hold TCP position fixed. Both position and orientation are endpoint contracts; the joint trajectory is not a straight Cartesian jog.

Status: implementation is frozen after coordinator review and independent offline acceptance. No device calls, installation or deployment during this work.

## Frozen candidate

Final source: `44f2f0aa68b8b7afcd18ef8c07abd5f94f3607079d65863fb4510d19e28a44e4`, 397 manifest files. Base: e8109e8a. Source is frozen. Terra ran a focused 36-test independent offline subset on prior source 13115877; it was not the full app or vendor suites. Subsequent test-only changes remove duplicate `app/test/test_basler.py` and update two stale fake constructors in native `test_cli.py`. Product files are byte-identical. The complete independent Basler test file remains. New source includes finite STEP worker/API/shared card, reused manual owner/cleanup/Stop, Basler provider preparation, short UI copy, stable independent tests and 35 exact fine-board assets. `BASLER_BOARD_PROVENANCE.json` verifies each new board file against the user-provided archive. Inherited immutable board assets remain. No IPC runtime data, environments or caches are packaged.

Robot TDD evidence includes missing-route RED, exact-plan/local-frame GREEN, posting/error race RED/GREEN, definite-rejection correction RED/GREEN and immediate confirmation reset RED. The focused independent subset was GREEN 36/36 on 13115877 (`results/final-13115877.txt`); it did not cover the full suite. No passing tests were rerun for the duplicate-only deletion. `MANIFEST_DELTA_13115877.json` records the verified sole change; prior manifest/archive are retained for provenance. `STE_COPY.md` records final before/after examples. The source archive and manifest were verified locally. No real robot, MCU or camera calls, SDK installation, service restart or deployment occurred in this implementation pass.

## Pre-existing native fixture correction

Affected native checks exposed two stale fake constructors in `vendor/ur10e-handeye/tests/test_cli.py`. They rejected the existing `collision=` keyword, including on the e810 baseline. Only these two lambdas now accept `**kwargs`; their zero-motion and camera-plan assertions remain unchanged. Product bytes match e37578a0. `results/affected-handeye.txt` records the failure; Terra reran only the two affected checks. Both passed in the pinned laptop venv with guard, isolated scratch test against unchanged staged17b product (2/2 GREEN;3.13s). Previously passing 36/78 and 55 cases are not rerun. Prior source archives and manifests remain available. `MANIFEST_DELTA_e37578a0.json` verifies the sole test-file change. Generated pytest caches are excluded from the release.

The same pre-existing tests also expected an older handled exit code/message and halved camera rotations. Their final expectations now match existing reviewed behavior: empty collision-clear plan exits2 with its stderr diagnostic; defaults are25deg tilt/30deg roll,30mm probe,near0.9/far1.1. The latter test name now says `reviewed_camera_rotation_defaults`. Both zero-motion checks remain. Final source was frozen only after the focused2/2 GREEN. `MANIFEST_DELTA_17b22938.json` confirms only this test file changed; product code remains byte-identical to e37578a0. No prior passing suites were repeated.

## STEP speed display

Coordinator screenshot review found binary float noise in the STEP summary speed (`3.0000000000000004 degrees/s`). The sole runtime delta from b59d3579 formats this display with `toFixed(2)` (`3.00 degrees/s`). Backend, data and motion behavior are unchanged. Prior b59d artifacts are retained. Terra will verify one focused screenshot-flow assertion; no backend or full-suite rerun is required. The corrected CLI evidence is `results/affected-two-corrected-cli.txt`.

## Installed release

The reviewed 44f2 release is installed. All 397 source files, three rendered pages and eleven served assets match. All 862 existing data files retain their hashes. Exactly 35 reviewed Basler boards were appended. Cached ownership is idle and clean. Pypylon 4.2.0 was installed offline in the IPC venv without dependency changes, SDK import or enumeration. ROOT_REVIEW.md and activation/activation.json record paths, hashes, process identity and limits. No physical movement or camera validation occurred.
