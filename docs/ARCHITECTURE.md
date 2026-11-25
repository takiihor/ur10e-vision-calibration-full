# Operator app and vehicle executor

## Installation roles

The mains-powered controller runs the existing Anodizing Operator app. It is
the only operator control surface and owns recipe review, scheduling and
basket records. The battery-powered vehicle IPC owns hardware access and runs
one accepted job at a time. The IPC keeps that job's instructions, checkpoints
and results. The controller keeps the recipe and the plan cursor.

| Installation role | Responsibility |
| --- | --- |
| `orchestrator` | Runs the existing UI and the programme loop. Sends versioned jobs, tracks their results and maintains the controller lease. |
| `executor` | Runs the stable IPC service with a stable data directory. Owns the wired AGV bus and performs admission, interlocks, device retries and local recovery. |
| `status-display` | Shows a cached executor snapshot on a dedicated IPC listener. Serves the kiosk without opening devices or exposing control routes or credentials. |

Use one role setting per installation. Roles choose the services and routes
available at each installation. Device-level interlocks apply independently
of the role. Keep standalone Calibration, Teaching and Pick/place entry
points, data and native cores usable independently. Any standalone path that
can affect the same hardware must respect its exclusive owner.

Phase 1 uses the existing `StationVisit`, wired AGV driver, `JobOwner` and
`JobStop` in the executor. The controller uses the same job interface for
manual station requests and recipe travel. Release the raw serial bridge's
bus ownership before starting the executor. Verify live status through the
executor before retiring the bridge. The controller reaches the IPC loopback
job service through a tunnel. A service-only credential restricts control
access. The kiosk's listener serves cached state, including its age, without
querying the bus. Install the executor under a separate system service identity
so the graphical kiosk account cannot read its token or manage its service.

The local radio is an optional backup and starts disabled. Change transport
only after disarming, disconnecting and positively verifying that the former
owner has released the bus and cannot issue commands. Treat controller-local
radio as an explicit, mutually exclusive maintenance mode.

## Job contract

| Operation | Required behaviour |
| --- | --- |
| Health and capabilities | Report protocol major, build, supported job types and versions, and executor boot identity. Keep cached status readable across incompatible builds and reject incompatible control. |
| Status | Report current and by-ID job state and result, last confirmed step, outstanding intent, device state and freshness, hold or recovery reason, and dispatch permission. |
| Connect, disconnect, arm, disarm | Require explicit control with fresh compatible state. Bind approvals to the executor boot, controller session and Stop generation. A new connection grants no motion authority. |
| Submit | Persist the run, basket, ordered steps, recipe and limits revision, job ID and immutable payload on the controller before sending. Persist acceptance on the IPC before execution. A repeated ID with the same canonical payload returns its existing state or result. A changed payload conflicts. A different ID while occupied returns busy. |
| Heartbeat | The controller service sends advancing heartbeats every 1 s. The executor expires the active session 3 s after its last valid heartbeat, measured on its monotonic clock. Ignore duplicate, delayed and prior-session renewals. |
| Stop | Send directly to the executor with priority even while busy or disarmed. Latch Stop against concurrent admission independently of the normal queue and routine storage. Allow same-ID retries and report receipt, halt attempt and fresh confirmation separately. Confirm after active work retires and fresh evidence verifies the basket, equipment and AGV at a safe hold. |
| Acknowledge and reconcile | Merge progress by job ID and sequence. Commit the basket record and next cursor together before acknowledging receipt. Preserve IPC deduplication across releases and through the retry window. Require explicit recovery with fresh physical evidence before resuming from an agreed boundary. |

Jobs progress from accepted to running and succeeded, or to stopping and
confirmed stopped. Held, failed and interrupted or unknown outcomes retain
the reason and last confirmed location. An outcome and permission to
dispatch are independent. Before an irreversible effect, persist intent.
Record the resulting evidence and advance the durable progress sequence only
after confirmation. An unconfirmed effect prevents further motion even if
the scheduler supplies a new job ID. The controller retries delivery using
the original ID. Only the executor can make bounded device retries under
local interlocks. For a station visit, the device acknowledgement proves
receipt. Arrival needs fresh matching station and stopped feedback. The
controller records a manual visit with its original boot, session and
Stop generation before sending it. Katana looks up that ID through a lost
reply or restart. It may retry a missing submission only under the
original authority. The AGV page offers reconciliation by that ID when
the outcome is unknown or the original authority changed and the result
is missing. Verify the simulated hold, record the disposition and then
acknowledge the result. Katana archives the original ID, outcome and
disposition in `executor-manual-history/<job-id>.json` before clearing
the current intent.

A paired `dip` job identifies both step IDs, the source and destination,
tank and taught-endpoint references, expected basket state, time bounds,
and required final hold. The complete place, dwell, lift and transfer path
runs on the executor with simulated devices in Phase 1. Production dip
handling, live robot integration and taught-endpoint resolution are Phase 2.

## Loss of link, power and feedback

A successful handoff requires a verified basket location, gripper and robot
condition, stopped AGV and the required tank or equipment state. A job may
end in a verified rinse hold or park with the executor disarmed. The
controller loses dispatch permission when the lease expires. Job continuation
depends on the local evidence and the pre-authorised boundary below.

| Event | Response |
| --- | --- |
| Controller restart | Reload the plan revision, basket, pending Stop and outstanding job ID. Compare executor identity, checkpoints and terminal results before advancing the cursor. Reconnection alone grants no motion authority. |
| Executor restart or power loss | Boot disarmed with a new identity. Mark execution unknown and retain the last location, last intent and unconfirmed effects. Require recovery for a missing or unreadable active record. Reconcile vehicle, arm, gripper, basket and equipment against fresh physical evidence before re-arm. |
| Wi-Fi loss | Before dispatch, stay at the verified boundary. An accepted visit may finish its authorised leg while local travel checks remain valid. An accepted dip may reach its specified rinse or park while timing, destination and equipment checks remain valid. Hold and disarm at the boundary. If the path fails qualification, alarm locally, inhibit dispatch and record the actual or unknown basket condition for local recovery. |
| Low battery or stale feedback | The IPC admits work only with valid feedback and measured reserve for the worst case, including transfer and recovery. On a low reading during a job, use the approved stage response and inhibit the next job. Critical cutoff or brownout requires local intervention and reconciliation. |
| Stop while unreachable | Inhibit dispatch immediately, persist Stop intent and show **Stop unconfirmed**. Deliver it before re-arm or dispatch on reconnect. During a partition, the local physical e-stop is the immediate intervention under site procedure. |

Local faults, Stop and e-stop take precedence over job completion and quality
limits. Treat the 1 s heartbeat and 3 s expiry as commissioning candidates.
Qualify the live timeout against network jitter, the acceptable detection
delay and the local response. Track serial freshness, pose age, PLC sample
age and chemical deadlines separately. Measure elapsed intervals with a
monotonic clock per boot and record UTC plus any source-clock uncertainty.
After reboot, preserve existing evidence and mark missing intervals unknown.

## Tank timing and limits

The line PLC owns process timing. The controller mirrors the setpoint,
timer present value, done, basket-present and equipment state for scheduling
and records. Establish a fresh baseline and correlate placement, timer
start or reset, advancing feedback, done and equipment state for the current
basket. Budget polling, sample age, transport, decision, lift and transfer
against each process limit. Treat duplicate, stale and out-of-order samples
as insufficient evidence of a new edge. Lifting on a signal relayed by
the controller remains simulation-only. A future live dip needs qualified,
cycle-specific signal evidence on the IPC through a partition, or an
independently approved fallback with a deadline and defined action on
expiry. This still applies if the controller later moves to wired networking.

Pair each time-critical process with its rinse in one job, including etch,
desmut and anodize. Qualify the pickup, transfer and receiving hold before
admitting immersion. Set a maximum rinse hold and a local response if it
expires. The PLC equipment cutoff and its timing need separate confirmation.

These values are **proposed, to confirm with the process owner**. Use them
as editable draft settings in the existing UI. Keep physical tank and transfer
identity private. Each row records allowable overstay, minimum dwell and
maximum time from confirmed lift to next immersion.

| Tank role | Tolerance | Overstay | Minimum dwell | Air time |
| --- | --- | --- | --- | --- |
| Etch | Tight | 5 s beyond setpoint | Setpoint less 5 s | 15 s |
| Desmut | Medium | 30 s beyond setpoint | Setpoint less 10 s | 30 s |
| Anodize | Medium | Lift within 60 s of cycle-confirmed PLC done | Early lift flags thin coating. Confirm the minimum. | 30 s |
| Seal | Loose | Needs confirmation | Full setpoint | Confirm the successor or a terminal transfer exemption |
| Ultrasonic clean | Loose | 60 s beyond setpoint | Full setpoint | 60 s |
| Rinse | Loose | Needs confirmation | Full setpoint for drag-in protection | Resolve from the actual following tank |

Save draft edits by recipe revision and show effective values in seconds,
the source revision and DRAFT or FINAL state at review. Convert early
allowances to an absolute minimum. Require finite bounds with
`0 < minimum ≤ setpoint`, nonnegative overstay and positive applicable
air-time limits. Keep confirmed-PLC-done deadlines distinct from
setpoint-based allowances. Resolve inherited limits and require evidence
for a terminal transfer exemption. Finalize separately after the process
owner resolves all applicable bounds and cycle rules. Editing a final revision
creates a new draft. Snapshot the reviewed revision into a run and its jobs.

Record actual dwell, air time, PLC cycle evidence and equipment state for
each basket and tank. Count each rinse exposure once, including time in a
hold after link loss. A limit breach raises an alarm, flags inspection and
holds further dispatch at a verified boundary. The job can complete only the
approved safe action while Stop stays immediately available. Record the
operator's disposition. Scrap decisions remain with the operator.

## Phase acceptance and next phase

Phase 1 starts with an end-to-end 12-step simulated basket through the
existing UI and versioned job interface, including draft limits, paired
rinses, holds and recovered records. Exercise restart of either host,
lost replies and conflict detection, stale feedback, low battery, incompatible
builds and Stop during a partition. Keep real hardware checks to live
status, explicit arm and disarm after offline acceptance. The IPC service
name and data directory stay stable across version changes. Preserve prior
installation data and job evidence for migration or rollback.

Phase 2 puts real robot jobs, cameras, tower light and other vehicle devices
behind the IPC executor. The controller provides the live line integration.
Equipment sequencing, credential moves and production timing qualification
also belong to this phase. Require qualified local pose/interlock evidence
and measured stopping and travel times before live station visits. Require
cycle-specific local PLC evidence or an approved independent fallback before
live dips. Both require defined, observable holds and recovery responses for
power loss, a blocked destination and Stop during a partition.

## Open decisions

1. Who finalizes recipe revisions and releases inspection holds? Confirm
   one-basket operation and its input, output and park conditions.
2. Which local signals prove immersion, lift, a new PLC cycle and equipment
   off for each process? Confirm reset, latch, cutoff and interlocks during
   a partition or IPC power loss with the supplier.
3. Which process and rinse pairs share a reachable station? For the rest,
   qualify the route, receiving tank, transfer budget and maximum rinse
   hold. Assign a local responder to a blocked destination or IPC power loss.
4. Confirm the disputed anodizing dwell and the reason for its additional
   pad. Resolve the rinse's zero setpoint and the initial level alarm.
5. Qualify battery reserve, critical response and loss-detection delay.
   Confirm local Stop and e-stop indications and recovery steps.
6. Confirm who operates the radio backup and who verifies physical
   isolation of the former owner before changeover.

## Phase 1 operation

Set `ANODIZING_ROLE` to `orchestrator`, `executor`, or `status-display`.
The installed executor uses its own stable data directory. Set the version
label with `ANODIZING_EXECUTOR_BUILD` and give the service its token through
`ANODIZING_EXECUTOR_TOKEN_FILE`. Its control port is loopback. The separate
loopback display port serves cached status to the kiosk. A display-only
installation can read that display port using `--status-display-port`
as its source and `--port` as its own listener.

Set `ANODIZING_EXECUTOR_PORT` on the orchestrator to the local forwarded
control port and `ANODIZING_EXECUTOR_TOKEN_FILE` to its private service
token file. The orchestrator sends the lease heartbeat independently of
the browser. Its existing AGV page uses the backed-by-executor Wired
link. In commissioning the executor accepts Connect, a status query,
software Arm, Disarm and Disconnect. Arm checks fresh stationary status
and writes no command. Live station visits and dips remain fenced.
Software Stop can latch even when disarmed. The commissioning transport
cannot attempt a wire halt. Use the physical e-stop for intervention.

The shipped production role uses only the wired driver. The backup serial
radio requires a deliberate maintenance changeover to the existing serial
mode on the controller. Disarm, disconnect and verify physical isolation
of the executor before removing its forwarded endpoint from the
controller configuration and enabling local serial support. Restore the
executor path only after verified radio isolation.

Set `ANODIZING_PROGRAMME_FILE` to a private JSON list of ordered steps
with `id`, `tank`, `role`, `station` and positive `setpoint` in seconds.
The existing Pick/place screen exposes the simulated basket programme.
For a two-host or two-process simulation, run the executor with
`--fake-executor` and point the orchestrator at its authenticated loopback
or forwarded port. The programme and AGV page share that client's lease.
Start and Resume require the boot, controller session and Stop generation
returned when the operator reviewed the programme. The simulation job
versions and review are both checked before the programme claims the
executor. A commissioning-only link disables these buttons and still
allows Connect to obtain recovery feedback after a previously held run.
The programme keeps its own journal and uses the same HTTP API as the
commissioning link. To run an isolated local simulation alongside the
commissioning link, set `ANODIZING_LOCAL_FAKE_PROGRAMME=1`. A standalone app
with no executor endpoint can simulate locally. Configuration of this
programme grants no live travel or chemistry permission.

Provision `fake-plc-setpoints.json` in the fake executor data directory.
For local simulation, use the `simulation-executor` subdirectory of the
operator data directory. This private JSON object maps synthetic tank IDs
to their positive PLC setpoints in seconds. Review and provision it
independently of the programme file. The fake PLC starts its timer from
this setting. Admission refuses a dip when the setting is missing, zero
or different from the reviewed plan. The commissioning service does not
load this file or offer dips.

A motion-authorizing click carries a request ID, the reviewed controller
cursor and the existing Stop authority. Katana consumes the cursor and
persists the request before dispatch. A repeat with the same body refers
to the original visit or basket. To start another basket with the same
label, review the current controls and issue a new Start. A delayed,
previously unseen request with a superseded cursor needs a fresh review.
Stop can interrupt a pending operation.

The IPC keeps an ordered inventory of accepted job IDs in
`.executor-accepted.json` and durable pre-admission refusals in
`.executor-refused.json`. The active marker and individual job files
provide independent evidence. If either inventory is damaged, inhibit dispatch,
stop the executor and preserve the damaged bytes. Restore the complete inventory
from a verified copy covering the last acceptance or refusal before the fault.
Compare its digest against the frozen copy and verify the latest marker and
controller history before restarting. An older backup can omit consumed IDs.
When the latest inventory cannot be established, retain the fence and recover
the missing evidence before restarting work. Inspect a lost reply using its original
job ID. A physical effect can occur just before power fails and before
its confirmation reaches storage. That interval requires reconciliation.
Only a qualified immersion, PLC completion and confirmed lift on the
same boot establish a completed dwell. A remaining transfer or rinse
can stay uncertain while the completed dwell appears in the basket.

At the first station job the fake executor verifies the input handoff,
binds the basket to the run and checks that the first dip starts at input.
An independently verified output hold for a previous run allows the
simulated unload and next input load, including when two successive baskets
have the same label. A different basket at input or an unverified hold
blocks the load before station travel.

Start at the reviewed input, inspect the effective draft revision, and
start the simulated run. The run's revision and result history are durable
in the orchestrator data directory. Record an explicit disposition for
inspection or uncertain execution, including a missing executor result.
Stop changes dispatch permission independently of the basket outcome.
The urgent area distinguishes receipt, halt attempt and confirmed halt.
An outstanding Stop survives reload. Once a successful Arm clears the
current Stop, the prior confirmation appears as history.

Actual dwell is the interval between confirmed immersion and lift on
one executor boot. The record keeps the PLC cycle, elapsed time, exact
simulated done edge and sample time. For anodize, overstay runs from the
qualified done edge to lift. A late sample therefore cannot hide either
immersion or post-done time.

The fake executor keeps device state in a separate `.fake-devices.json`
file. A device effect invalidates the previous snapshot before changing
state and replaces it after the change. Missing, unreadable and incomplete
snapshots with a job history leave the simulated state unknown. An intact
snapshot still has to show a stopped AGV, a released gripper, a verified
hold and equipment off before disposition. In a recovery exercise, the
simulated plant needs an independently verified observation of station,
basket, location, gripper and equipment after an incomplete effect.
A new executor never supplies this evidence from its default input state.
For a missing or unreadable simulated device snapshot, keep the simulator
stopped and preserve the original file if present. Obtain the current
state from the independent fake plant fixture or an explicitly staged
offline reset, and record how and when it was verified. In the fake executor
data directory, prepare a private regular JSON file with these keys:

| Keys | Verified observation |
|---|---|
| `station`, `stopped` | Station number (0–255) and whether the AGV is stopped |
| `location`, `gripper`, `basket`, `run_id` | Robot and basket state. Use null for an absent basket or unknown run binding |
| `cycle`, `equipment_on` | Nonnegative PLC cycle number and whether tank equipment is on |
| `reserve_s` | Finite nonnegative usable simulated autonomy in seconds, or null if unknown |
| `recovery_input` | Object with `source`, `observed_utc` (Unix seconds) and `disposition` describing the independent verification |

Move the reviewed file into place as `.fake-devices.json` on the same
filesystem, restart the simulator and record the controller's disposition.
The executor re-observes the state and requires a safe hold before release.
Keep the safe-hold evidence with the original data. This file procedure
applies only to offline simulation.
The snapshot's `reserve_s` field records usable simulated autonomy in
seconds. Unknown or stale readings block admission. An admitted job needs
the configured fake travel, dwell and transfer durations plus a synthetic
10-second recovery margin. The simulator consumes those durations and
records the available and required reserve at admission and at each stage.
A depleted supply stops dispatch and retains the earlier confirmed stages,
including on executor restart. These values do not establish a physical
battery budget or brownout response.
Katana records the new observation with the job ID, executor boot and
observation times. The original job outcome remains in the basket history.

If `.executor-active.json` is unreadable, keep the executor inhibited.
Identify the outstanding job from the controller's durable programme or
manual intent and verify the simulated devices independently. The AGV
page offers executor recovery for an unreadable marker or a reported job
fence. Use the original job ID from the current manual intent, the
durable manual history or the retained programme record. When a programme
owns the outstanding intent, use its disposition. If the original ID is
missing, restore the controller journal or known backup before release.
Pass the actual `job_id` and `repair_active_marker: true` with the
disposition to the authenticated reconciliation operation. Set `missing: true` if
the result is missing, or `observed_only: true` if verifying a terminal
hold between jobs or a previously recorded terminal job. The executor
preserves the original marker bytes in a sibling `.unreadable` file and
atomically replaces the marker after saving the evidence. For an orphan
job, Katana records the current event in `executor-recovery.json` and
archives acknowledged evidence in `executor-recovery-history/`. An
unresolved job or job file keeps its independent fence. If the
job ID cannot be established, preserve the files and leave dispatch
inhibited for maintenance review.

For programme recovery, compare both the basket label and the run ID
with the observed hold. A replacement simulator's initial empty input
does not establish the previous basket's location. When independent
evidence identifies an absent or different basket, the operator can
record the original basket as unaccounted for. Its record then carries
no final hold. The simulator leaves a different basket in place.
Closing a verified occupied input or park hold records the disposition
and verifies the empty input before loading a new basket. A terminal
success followed by a lost IPC job file retains the earlier result
alongside its subsequent by-ID reconciliation.

At each station leg the IPC checks the current local travel interlocks.
A basket in a tank, tank equipment still on, or unknown feedback blocks
the leg. The commissioning-only wired installation has no travel
permission. A nonfinal lift with an air limit leaves a local deadline.
Expiry can flag the basket while Katana is disconnected. If clocks span
different IPC boots, keep elapsed time unknown and retain the inspection
flag until the operator accounts for the exposure.

The wired commissioning service journals each raw received chunk and
transmitted frame in its private data directory. Keep live capture out of
version control. The offline executor suite covers the job contract,
process loss, lease expiry, the wired path with a fake UART and pose source,
and refusal of an unsupported control over the display listener.

The executor reads `ANODIZING_HEARTBEAT_S` and `ANODIZING_LEASE_S` at
startup and advertises both intervals in health. The defaults are 1 and
3 seconds. Both must be finite and positive, and the lease must exceed
the heartbeat interval. Both controller paths use the advertised cadence.
Recordings take their connection ID, transport settings and executor boot
from the active AGV adapter when recording starts.
