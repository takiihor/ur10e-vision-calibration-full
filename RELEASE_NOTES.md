# Release notes

## 18 November 2026

- **F11 robot ownership.** Manual cleanup, Teacher close and teaching abort keep
  the shared job owner while a timed-out SDK worker is still alive. Teacher
  abort waits for workers registered during cleanup, including native motion
  workers retained by the Teacher service. Cleanup persists execution uncertainty
  and waits outside the owner lock. A failed fence write retains ownership and
  uncertainty in memory.
- Standalone modules validate bounds and camera inputs, require explicit or
  matching saved selections, preserve operator edits during Teaching prefill
  and revalidate cached assets. Basler Teaching receives the reviewed motion
  bounds. Continue to Teaching fills the selected calibration's TCP for
  operator confirmation. Teacher defaults accept new project paths inside the
  data directory and report invalid camera providers. Basler capture requires
  exposure and gain. Offline solves use recorded images without requiring those
  fields. Token-bearing pages use `no-store`. Launchers accept the documented
  interpreter configuration.
- The [verification skill](.agents/skills/verify-anodizing/SKILL.md) exercises the
  real UI service over isolated loopback HTTP. Doctor checks startup, idle
  ownership and disabled hardware. Five read-only journeys cover navigation,
  Calibration setup, Teaching setup, Pick/place availability and tool-preview
  assets. Tool preview saves the example scene as `collision-scene.example.json`.
- [Encoder-plate tools](tools/ENCODER_INTRINSICS.md) require explicit Basler
  serial selection, capture native Mono8 and fit intrinsics offline with held-out
  views and residual diagnostics. The fitter checks the capture serial when
  available and rejects byte-identical images shared by the training and holdout sets.
- [Provisional Basler intrinsics](review/results/basler-encoder-intrinsics/README.md)
  use restricted `k1` and `k2` with 0.340 px RMS over five holdout views. They are
  review artifacts for the recorded optical settings and require metric validation.

Verification is software-only. The bundled tests use fake hardware, loopback HTTP
and offline fixtures. The IPC, robot and AGV, physical stop timing and metric
accuracy remain untested. The unchanged vendor test
`test_tcp_change_or_rejected_stop_during_motion_is_reported` can intermittently
record `stopped` where it expects `failed`.

## 13 November 2026 review fixes

This revision addresses the review findings in the packaged bundle. It is still a
software review artifact, not a hardware qualification.

### What changed

- All bundled Python suites now pass from the package root. A single runner is at
  `src/anodizing-modules/tools/test-all.bash`.
- Site-specific values were removed from the product code and browser pages:
  robot address, collision-scene UUID, ±363° bounds, payload hash, camera serial,
  `tank01-new` project, and the named teaching board. Optional values can be
  supplied by an operator-owned `site-profile.json`.
- Teaching and robot-control defaults are discovered from files under the data
  directory and can be overridden by the profile.
- Manual worker and Teacher shutdown waits are bounded. A stuck native worker
  fences the job as execution-uncertain instead of blocking forever.
- Permission failures return HTTP 403. Static assets use an in-memory cache with
  ETag/Last-Modified revalidation; robot-model change detection no longer
  re-reads the model for every preview request.
- App dependencies, a CI workflow, a `.gitignore`, and an external default data
  directory for `run-ipc.bash` were added.
- Vendor versions carry a local `+anodizing.1` suffix and explicit license
  notices were added where upstream provided none.
- Offline vendor baselines, a per-file delta mapping and `provenance/verify.py`
  were added. `REVISIONS.txt` no longer claims the vendor trees are unchanged.
- Test corrections: stale camera kwargs, mock cleanup, `cv2`/`sys.modules`
  isolation, subprocess import path, full-pose browser move counts, missing
  external fixture, replay/CLI ordering, and charuco-generator config paths.
- `review/results/final-13115877.txt` is labelled as a 36-test focused subset,
  not full-suite acceptance.

### Limitations and open items

- **No hardware validation.** No robot, camera, MCU, PLC, IPC service or physical
  stop was exercised while preparing this revision. Offline tests do not
  establish clearance, timing, accuracy, grip or safety.
- **Vendor WIP was not reconciled.** Local development clones contained
  uncommitted Basler follow-up work, pick/place manual/poses/UI edits, hand-eye
  collision differences, and an A3/40 mm board change. Those edits are not in
  this bundle. The owner must identify the intended files before merging.
- **The vendor trees are not the pinned baselines.** They contain review changes,
  generated assets and build files. `provenance/BASELINES.json` records every
  difference; do not describe the bundle as an unmodified upstream snapshot.
- **Licensing is unresolved for most vendors.** Only `charuco-generator` includes
  an upstream license. The other vendor notices state that no redistribution
  permission is granted. Resolve licensing before publishing or redistributing.
- **Site values are operator input.** The bundle cannot know the correct robot
  address, scene, workspace, joint bounds or tool box. A missing or incorrect
  profile fails closed or asks the operator to enter values; it does not supply
  safe defaults.
- **The historical review evidence is partial.** The 36-test file is a focused
  subset. The earlier review records also refer to a 397-file release whose
  hashes no longer describe this revised source. Use the regenerated manifests in
  this package.
- **CI browser coverage can skip.** GitHub-hosted runners may not have Chrome;
  the browser integration tests then skip. Run them on a prepared workstation
  before release.
- **The IPC data archive is large and sensitive.** The local packaging bundle's
  `state/ipc-data.tar.gz` is about 537 MB compressed (647 MB uncompressed, 897
  files) and contains site projects, scenes and calibration data. It is omitted
  from this public source repository because it exceeds GitHub's per-file limit
  and is confidential operational data. See `state/README.md`.
- **No dependency lockfile beyond pins.** `app/requirements*.txt` pins the direct
  packages used by the tests. Transitive resolution still depends on the package
  index or a prepared offline wheel set.

### Verification performed for this revision

Final run on 13 November 2026 with Python 3.12.16 and pytest 8.4.2:

- app: 333 passed, 8 skipped
- ur10e-handeye: 270 passed, 1 skipped
- ur-pick-place: 238 passed, 9 skipped (includes the opt-in browser checks)
- charuco-realsense: 120 passed
- charuco-generator: 203 passed
- provenance verifier: all four archives and every recorded delta verified

The commands can be reproduced with:

```bash
cd src/anodizing-modules
ANODIZING_PYTHON=/path/to/python ./tools/test-all.bash
cd ../..
python3 provenance/verify.py
```
