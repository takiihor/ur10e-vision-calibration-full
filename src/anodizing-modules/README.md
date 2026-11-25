# Standalone anodizing test modules

These are three standalone web test pages. They do not require ROS, Qt or RViz. The Interface PC runs the service and owns the camera, computations, jobs and files. The laptop uses a browser through an SSH tunnel. Port 8870 is the default. App integration is deferred until the standalone IPC/laptop tests pass.

## Install for review on the Interface PC

Review and unpack the source bundle into a new directory. Do not replace the existing environment or data. Set these shell variables to the unpacked bundle and the prepared environment:

```bash
export MODULE_BUNDLE=/path/to/anodizing-modules
export ANODIZING_PYTHON=$MODULE_BUNDLE/.venv/bin/python
export ANODIZING_DATA_DIR=$MODULE_BUNDLE/data
cd "$MODULE_BUNDLE/../.."
sha256sum -c CONTENTS.sha256
cd "$MODULE_BUNDLE"
./tools/probe.bash
```

The probe imports software but does not enumerate or connect to devices. It requires NumPy, OpenCV with modern ChArUco APIs, and the bundled modules. It reports the camera and RTDE libraries separately. The prepared IPC environment was reported by the coordinator; this bundle does not contain a virtual environment.

The app dependency pins are in `app/requirements.txt` (development/test in
`app/requirements-dev.txt`, optional device libraries in
`app/requirements-hardware.txt`). The vendor packages are imported from this
bundle through `PYTHONPATH`; they do not need to be installed. For a fresh
environment:

```bash
"$ANODIZING_PYTHON" -m pip install -r "$MODULE_BUNDLE/app/requirements-dev.txt"
```

If `ANODIZING_DATA_DIR` is not set, `tools/run-ipc.bash` uses
`$XDG_STATE_HOME/anodizing-modules/data` (or `~/.local/state/anodizing-modules/data`),
not the source tree. Copy or select reviewed board folders explicitly.

If the data directory does not already contain the two selected board folders, copy them without overwriting existing data and compare the manifests:

```bash
mkdir -p "$ANODIZING_DATA_DIR/boards"
cp -an "$MODULE_BUNDLE/data/boards/." "$ANODIZING_DATA_DIR/boards/"
sha256sum "$MODULE_BUNDLE/data/boards/calibration_stage2/board_manifest.json" \
  "$ANODIZING_DATA_DIR/boards/calibration_stage2/board_manifest.json" \
  "$MODULE_BUNDLE/data/boards/stage2_charuco_realsense_80mm/board_manifest.json" \
  "$ANODIZING_DATA_DIR/boards/stage2_charuco_realsense_80mm/board_manifest.json"
```

Each corresponding pair must match. Leave existing imported calibrations and projects in place. File fields are IPC paths relative to `ANODIZING_DATA_DIR`, or absolute paths within it. Symbolic links in project and session folders are refused.

## Optional site profile

No site identifiers or safety values are compiled into the service. To prefill
reviewed values, place an optional `site-profile.json` in the data directory. It
is read only; it does not grant permission or approve motion. Paths must resolve
inside the data directory.

```json
{
  "teacher": {
    "project": "teaching/my-project",
    "calibration": "calibrations/my-camera/calibration.json",
    "robot_ip": "controller address",
    "tcp": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "target": {
      "kind": "charuco",
      "board": "part-board-name",
      "board_manifest": "boards/my-board/board_manifest.json"
    },
    "camera_provider": "realsense",
    "motion_model": "planar"
  },
  "robot_control": {
    "robot_ip": "controller address",
    "tcp": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "collision_scene": "collision/reviewed-scene.json",
    "workspace_min_m": [-0.9, -0.9, 0.0],
    "workspace_max_m": [0.9, 0.9, 1.2],
    "joint_bounds_rad": [[-6.283, 6.283], [-6.283, 6.283], [-6.283, 6.283],
                          [-6.283, 6.283], [-6.283, 6.283], [-6.283, 6.283]],
    "joint_bounds_source": "Reviewed controller configuration and date",
    "joint_speed_deg_s": 3,
    "teaching_project": "teaching/my-project"
  }
}
```

Use values measured or read from the actual controller. The example bounds are
placeholders, not a recommendation.

Workspace and joint bounds must contain finite numbers. An invalid Teacher
override that fails validation returns HTTP 400 with the field error, so a
partial override is not presented as a successful setup. Missing or unreadable
profile files still leave ordinary discovered defaults available. With no
matching taught project or explicit profile selection, calibration, robot and
TCP remain unselected placeholders; available calibration files are listed for
the operator to choose. Review the TCP before confirming it.

Token-bearing pages use `no-store`; scripts and styles use `no-cache` with ETag
revalidation. Invalid Basler exposure or gain entries stop form submission;
enter the intended values instead of relying on a fallback.

## Start and connect

On the IPC, start offline first:

```bash
"$MODULE_BUNDLE/tools/run-ipc.bash"
```

In a second IPC terminal with the same variables, run the simulation-only smoke client:

```bash
"$ANODIZING_PYTHON" -B "$MODULE_BUNDLE/tools/smoke.py"
```

It refuses a service started with hardware opt-ins. It creates a uniquely named `smoke/` folder and checks a simulated teaching, PICK and shifted PLACE. The service binds only to IPC loopback. It validates Host, Origin, a process token and bounded JSON requests. One service and one job owner are allowed at a time. Job records are under `.jobs`; accepted calibration and read-only jobs continue when a browser or tunnel disconnects. Supervised Teacher movement is the exception: it requires a live heartbeat and requests a stop after heartbeat loss. Reload and select the existing job ID. A repeated ID with the same request returns its stored state; a different request with that ID is refused. On service restart an unfinished job is marked interrupted. A server shutdown waits for workers to clean up; it interrupts a running calibration worker.

On the laptop, set IPC_SSH_TARGET to the SSH target from your private installation notes and start the tunnel:

```bash
./tools/tunnel.bash "$IPC_SSH_TARGET"
```

Use the laptop's copy of the bundle for that command. Then open `http://127.0.0.1:8870/`, or directly open `/calibration`, `/teaching`, or `/pick-place` at that address. Keep the IPC service and tunnel running. The request token changes when the service restarts; reload the page afterward.

## Use the pages

- **Calibration:** simulate a new session, solve an existing session, fit intrinsics, fit both, or verify using a separate session. The form retains tilt, roll, views and collision-scene fields. Automatic planning, collection and replay require `--allow-auto-calibration` and a confirmed collision scene for the selected scope. Planning opens controller control access. Collection and replay require separate phase approvals. The tool, cell and nominal UR10e editor is linked for viewing; it does not establish physical clearance.
- **Teaching:** the service ships no board name, project name, robot address or camera serial. Defaults are discovered from board manifests, saved projects and calibration files under the data directory, and can be overridden by an optional `site-profile.json`. ArUco is selectable. Read-only teaching uses the existing teaching project, camera/profile, calibration hash, TCP and marker checks. Enter TCP XYZ in millimetres and rotation vector in radians. The server converts XYZ to metres for the existing API.
- **Pick/place test:** choose the completed native teaching project, matching calibration and reviewed scene. PICK and PLACE are separate checked operations with fresh observation, exact route approval and separate grip/release confirmation. The explicit MCU buttons below control the gripper; a reply never confirms the item state or continues the motion sequence.

To enable only a camera board view, restart the service with `--allow-camera`. Select `calibration_stage2` and the intended camera serial. This action reads the camera without RTDE. It uses the existing board observer and returns an annotated preview, metadata and quality reasons. It does not qualify the board or a calibration. For a Basler camera, set **Exposure (µs)** and **Gain (dB)** in Setup. **Preview camera (no pose)** checks focus, exposure, gain and marker visibility without an intrinsics file; it does not produce a pose or qualify a calibration.

`--allow-teaching` separately enables camera and read-only RTDE access for teaching capture, teach and locate. `--allow-manual` separately enables pendant-positioned calibration collection using receive-only RTDE and the read-only Primary interface; it does not enable freedrive. Manual collection waits for each operator capture or stop reply. The service exposes automatic calibration only with `--allow-auto-calibration` and the scene checks. Teacher movement has its own opt-in described below. Only the fixed reviewed supply commands and the explicit MCU commands described below are exposed; arbitrary URScript upload is unavailable.

For an attended calibration trial, the selected scene can explicitly use
`collision_scope: "supervised_self"`, with `environment_complete: false` and an
empty obstacle list. This checks the arm and attached envelopes, not external
objects. The arm/attached-model confirmation, solver, controller limits, swept
paths and separate phase approvals still apply. Step 2 saves the tool envelope
to the scene used by Plan. The calibrated camera graphic remains separate.
Step 3 and each pending approval show the selected or accepted job scope.
Scenes without this field retain the complete-cell requirement.

For an explicitly selected robot model, pass `--robot-model` with a JSON path inside the data directory. The service displays its model hash in Plan and uses a frozen copy for automatic jobs. The preview uses the same model and hash. The selected model does not confirm the scene, tool geometry or cell. A changed model requires review before restarting the service.

`vendor/ur10e-handeye/src/ur_handeye/web/ur10e-visual.js` is generated geometry,
not hand-written source. It is produced from the supplied DAE visuals by
`vendor/ur10e-handeye/preview-build/export_visuals.py`; the browser bundle input
and pinned build dependencies are in that same `preview-build/` directory. Rebuild
and review it when the visual model changes.

Teacher alignment/return use checked joint motion. The separate reviewed vertical action and manual Pick/Place use the conditional Cartesian checker and explicit manual confirmations described below. The bundled nominal preview alone does not qualify motion. Camera-only observation, mock results, physical calibration and physical task validation are separate results.


## Teacher joint-space test candidate

Teacher Step 5 supports the current planar project: capture a fresh marker, review the calculated endpoint, confirm the physical area and selected scene, request one checked joint-space test, then review the result. Destination XY and orientation follow the taught marker. **Only destination Z equals capture height. TCP height can change during the joint move.** This is one stopped, unblended `moveJ`; it is not level Cartesian travel. Step 5 also provides a separately confirmed checked return to the saved observation joints, without marker detection or endpoint IK. The return binds the saved observation hash, selected scene/factory model and current TCP, and reuses the same checked joint worker, Stop and cleanup. Review the full joint path: TCP height can change. Full-pose validation and gripper actions remain unavailable. The separate taught-height descent below does not change the planar project or old Run behavior. After return, capture again before a validation test.

Read-only capture/review uses `--allow-teaching`. Movement additionally requires `--allow-teacher-motion`, `--robot-model` pointing to the selected factory model inside the data directory, and an explicitly selected confirmed collision scene. These flags remain off by default. No startup/device access is automatic. Joint speed is entered in degrees/s, from 1 to 6, and converted to radians/s for the SDK; joint acceleration is 0.1 rad/s². Existing workspace bounds apply to endpoints; no new Z=0 floor is added. Both controls show why movement is unavailable. The browser retains only the operator-entered scene path; it restores no confirmations or permissions. Return speed defaults to 3 degrees/s. Refresh/restart never starts or resumes movement automatically.

The unchanged native checker binds exact start/target joints to the selected scene/model. Controller FK is compared with that model, and fresh stationary/drift, TCP, calibration/grasp, capture age and controller safety checks must pass immediately before dispatch. `supervised_self` checks full arm and attached tool/camera/mount boxes; external objects are excluded. It remains supported without measured cell obstacles. Cell scope retains its existing complete-environment requirement. Unavailable, unresolved or colliding routes refuse movement.

One native Teacher session retains capture identity across browser requests under the existing JobOwner. Ordinary actions retain that session; device workers still use native acquisition/cleanup. Captures are single-use and expire after 60 seconds; restart never restores permission from saved capture files. Request IDs prevent duplicate dispatch. Stop and heartbeat are token-protected, bound to the current Teacher job and independent of normal job admission. The sidebar displays native preflight/checking/moving/stopping/error text and the actual job state separately.

Admission remains held until workers really finish cleanup. SDK calls can block; this does not claim bounded physical stop timing. A failed stop/cleanup or still-live worker sets an explicit execution-uncertainty fence, which survives restart through saved job/path evidence. `/api/status` exposes the fence and recovery notice. There is no automatic replay or clear-fault button: the coordinator must review physical state, worker termination and saved outcomes before a separately reviewed reconciliation. Files remain intact. A clean read-only failure does not invent a motion outcome.

After a cleanup timeout, the job retains ownership while remaining SDK threads
drain outside the owner lock. Manual cleanup, Teacher-close and teaching-abort
paths share this rule. A failed fence write records a storage error and keeps
the in-memory fence and ownership; it cannot skip the worker drain. If a worker
never stops, the job keeps admission until operator intervention. Review the
physical state and saved evidence before restarting the service.

Exclusion is within this module service and its workers, not controller-wide or cross-host. Unrelated legacy standalone programs do not consult this JobOwner. Do not run a competing controller. Native UR RTDE remains selected; this candidate does not migrate to the newer shared-controller runtime or ROS. See companion `HANDOFF.md` for exact source/evidence and review status. Offline tests do not establish physical trajectory, stop or alignment performance; deployment and user movement have not been performed by this task.


### Manual Pick/Place and separate taught-height descent candidate

The separate manual panel keeps independent pick-tag/place-tag selections (both must match the selected project's taught board) and explicitly reuses the original pick-taught marker-to-tool offset with separate provenance. Original grasp/reference/configuration data are never rewritten. The visible PICK / PLACE controls use the native checked sequence.

After coordinator review, IPC `--allow-manual-pick-place` with the selected factory model enables explicit manual requests; it stays off by default. Select the reviewed scene, configured operating joint bounds and source, workspace and speeds; confirm active TCP and that the held item fits wholly inside the selected scene's attached tool box. PICK and PLACE each obtain a fresh marker observation that meets the selected project’s quality checks, show the exact plan for another bound reply, align above by checked joint transit, descend straight base-Z with actual stopped XY/orientation held, pause for manual grip/release, and withdraw straight+100mm. No automatic gripper action, force search or held-state inference. MCU commands require separate button clicks. No moveJ substitutes for vertical legs and rejected hardware requests never run simulation.

Step5 additionally exposes one separately approved vertical descent after the current completed planar alignment. It binds current alignment/capture/grasp hashes and explicit confirmation that marker/dummy/rod have not moved. The full current taught height is read from saved data, not an incremental probe or old taught pose. Old Run only aligns. A later relevant task, return, reteach or restart invalidates descent eligibility. If a taught endpoint falls outside the reviewed workspace, review the bounds before proceeding. A workspace edit requires operator input and cannot change controller safety limits. Review and approve the exact new plan before motion.

Each vertical leg requires the full continuous-branch conditional clear_in_model certificate plus immediate exact binding recheck. Native joint chords and sampled IK never certify moveL. The checker keeps explicit floating numerical allowances and the total0.2degree model/tracking correspondence assumption. At most three representative controller-FK/safety samples detect mismatch, with progress/timing recorded; they do not prove whole-route correspondence. supervised_self excludes external cell objects. Full geometry checking still covers every accepted branch ball.

Accepted manual IPC tasks survive laptop loss and hold at manual boundaries; refresh/reconnect/restart never answers/replays. Only scene path and inspectionID are retained, never confirmations. Each phase reply binds job/role/observation/phase and is consumed once. Teacher's existing2second heartbeat policy stays unchanged. Separate per-leg certificates/outcomes survive withdrawal. Stop is latched before disk persistence; owner admission remains held while any native SDK worker is alive. Unconfirmed cleanup preserves durable execution uncertainty. Review HANDOFF.md and focused fake/native/browser evidence before activation. Offline checks do not validate physical motion or stop behavior.


### Explicit MCU commands — candidate 10 November 2026

Pick/Place has four buttons beside the existing MCU supply panel: `0 — Read sensors`,
`1 — Tight close`, `2 — Half close`, and `3 — Open`. Each click sends exactly one
ASCII byte through the already configured robot tool forwarder on TCP 54321.
There is no newline, CRC, ROS service, serial dependency or automatic grip sequence.
The saved native project's validated numeric robot address selects the endpoint.
The existing `--allow-manual-pick-place` opt-in is required.

The buttons work while idle, or while the same PICK/PLACE waits at its exact grip
or release prompt. The project, robot and project hash must match that parent.
The parent retains its native worker, ownership and prompt. While an MCU request
is pending, confirmation and new job admission are blocked; Stop stays available.
An MCU reply does not answer the prompt. Check the physical item and use the
separate operation confirmation before lift/retraction. Raw text, hex and FSR
sensor values are evidence of the reply, not proof that an item is held.

Anobot revision `4e9a298c94e07e1d26a47f7d615f957741dd7e2f` defines these bytes and
case-insensitive exact `Tight`, `Loose`, `Open` replies, and `FSR1:n/FSR2:n` status.
The adapter uses its 2 s read limit, 0.5 s write limit, 0.1 s quiet period and
128-byte reply bound. Queued input is discarded before each explicit command.
Only the saved reply is shown after opening or refreshing the page; GET never
connects to hardware. Request IDs are durable and exact retries cannot send twice.

A failed connection before send is recorded as not sent. After commands 1/2/3,
a missing or invalid reply leaves a durable execution-uncertainty fence. Command
0 is a sensor query: a failed reply records unknown status without creating a new
actuator fence. Existing uncertainty blocks all four commands. No request is
replayed automatically, including after restart. A successful reply cannot clear
another operation's uncertainty or storage fault.

This candidate keeps the robot's existing TCI settings. It sends no UART setup
script and changes no controller settings. Anobot's end-effector YAML says
9600 baud, 8 data bits, no parity, 1 stop bit; its robot launch default says
115200 baud. The pendant's actual TCI settings must match the connected MCU.
A reachable forwarder does not establish those settings or MCU compatibility.
Fake-socket and browser results are software evidence only; electrical behavior,
grip strength and physical outcomes require separate attended hardware checks.

### Finite automatic rod-transfer test

The standalone Pick/place page also supports one supervised automatic test. Select endpoint A's completed teaching project and calibration. Select a completed endpoint B project, or leave B empty to use A. Each endpoint keeps its own taught marker identity, marker-to-TCP relationship, saved observation joints, approach height and validation limits. The projects must use the same robot, calibration, active TCP and camera configuration. No grasp or teaching file is rewritten or retagged.

Enter **Transfers** from 1 to 100. One transfer is one pickup and placement: two transfers command A → B → A. Same-tank mode uses the same definition. Review the recipe, check the physical setup, then use the single approval checkbox and Start. The recipe digest binds the reviewed settings, teaching/calibration/manifest files and scene. Edits require another review. The live IPC worker retains one owner and native connection for the entire bounded run. The browser only submits Start, reads status and sends an explicit Stop; refresh never replays or resumes commands.

Before any arm motion, a read-only MCU command 0 must return a valid sensor reply. Before each operation, the worker admits that endpoint's saved observation joints through operating bounds, controller safety, workspace, controller/model FK and the existing whole-joint path check, then obtains a new stationary quality-gated observation. It computes and validates the fresh endpoint and approach, checks the joint transit to hover, and keeps the existing checked fixed-orientation base-Z moveL legs. The approach height comes from the selected project and is shown in the recipe; it is not silently replaced with 100 mm. The withdrawal is always straight +100 mm from the stopped endpoint, without shortening or a joint substitute.

PICK sends open command 3 at hover, descends, then sends tight-close command 1 at the endpoint. PLACE descends and sends open command 3 at the endpoint. The existing raw TCP protocol, exact reply validation, durable dispatch evidence and no-retry rule apply. A bounded settling delay defaults to 1 second after each actuator command (0–5 seconds in Advanced settings). This timing is unverified. No MCU ACK or FSR value verifies that a rod is held or released; there are no inferred sensor thresholds. Progress counts **completed commanded transfers**, and endpoint/item states remain explicitly unverified. Automatic ACK states are separate from operator-confirmed manual states.

Each operation and MCU child has separate evidence. Stop or any quality, path, transport, input, controller or cleanup error ends the whole test with item state unknown. No automatic release, recovery move, retry or restart replay follows. Post-send actuator uncertainty preserves the existing execution fence, including after process restart. Accepted bounded work continues on IPC if the laptop disconnects. Keep the physical Stop accessible and supervise the test. External manual MCU controls and power requests are blocked during the run. Advanced single PICK/PLACE retains its separate plan and grip/release confirmations; Teacher behavior is unchanged.

Offline fake-robot/camera/MCU tests cover sequencing, two native target identities, recipe compatibility and edits, quality/path refusal, Stop, exact request reuse and durable restart fences. These checks do not validate physical gripping, UART settings, timing, printed markers, robot placement accuracy or external cell obstacles. The existing `supervised_self` collision scope and Anobot UART configuration limits still apply. This candidate is not deployed by the source preparation workflow.


## Current operation controls (2026-11-10)

Calibration, Teaching and Pick/place put setup inputs, read-only review/capture controls and initiating action buttons in the main panel. The wider right-side Current operation card contains monitoring, recipe/setup and exact-plan approvals, physical confirmations, same-request retries and Stop/Cancel. Pick/place puts manual MCU gripper command buttons and supply controls in the main panel; cached MCU replies and stored-request inspection stay in Current operation. On narrow displays the card stacks above setup and scrolls internally; its heading and Stop controls stay visible while its contents scroll. Existing request ownership, payloads and confirmation requirements still apply.

## Tests and provenance

Run every bundled Python suite with one command:

```bash
ANODIZING_PYTHON=/path/to/python ./tools/test-all.bash
```

The runner imports the vendor trees directly, runs the app and four vendor
suites, and leaves the opt-in browser checks skipped when Node or Chrome is not
installed. It is also used by `.github/workflows/tests.yml`. The historical 36-test run covered a focused subset.

From the module bundle, verify the public vendor files using the trusted private
record and its baseline archives:

```bash
python3 ../../provenance/verify.py /private/baselines/BASELINES.json
```

The bundled vendor trees include review changes and are not identical to the
pinned baselines; the public mapping describes the included vendor files.
The release notes list work that was not reconciled.

Basler camera preparation and physical limits: [BASLER_PREPARATION.md](BASLER_PREPARATION.md).


## Robot steps (2026-11-10)

The Robot steps card is available on Calibration, Teaching and Pick/place. It does not require camera access, calibration or a teaching project. Enter the robot address, active TCP, reviewed scene, endpoint workspace and operating joint bounds. Saved test settings are a draft. The existing `--allow-manual-pick-place` flag and selected factory model also gate these finite joint steps.

Choose a base X/Y/Z increment of 1, 5 or 10 mm, or a tool-local rotation of 1, 2 or 5 degrees. A +/− click prepares one frozen start and endpoint. It does not authorize motion. Review the endpoint and pinned settings in Current operation. Check the physical area and approve once. The IPC checks the full joint path before one unblended moveJ. The TCP path can curve and change height or orientation. Workspace bounds apply to the start and endpoint. A tool rotation keeps the requested TCP endpoint position fixed. No gripper command is sent.

The shared IPC owner retains the worker through cleanup. Stop cancels pending approval or stops the accepted move. A disconnect does not repeat or resume work. Network failures leave the request blocked for inspection. A definite rejected request permits correction and a new explicit click. Dispatch evidence uses the existing `*.manual-path.json` restart fence. These changes require offline regression and browser acceptance before activation. Fake robot checks do not validate physical clearance, model accuracy or controller timing.


When a private site profile contains `agv.wired_port`, the web service offers
Wired RS-485 on both AGV pages. It uses bare outgoing frames and accepts bare
or radio-prefixed replies. This profile fences motion and recipes in every
transport. Use the private device path for the approved serial adapter.
Optional `agv.fault_descriptions` maps decimal fault codes to local descriptions.
