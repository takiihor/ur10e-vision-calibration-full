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
manual station requests and recipe travel. Retire the raw serial bridge before
assigning the bus to the executor. The controller reaches the IPC loopback
job service through a tunnel. A service-only credential restricts control
access. The kiosk's listener serves cached state, including its age, without
querying the bus.

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
| Stop | Send directly to the executor with priority even while busy or disarmed. Latch Stop against concurrent admission independently of the normal queue and routine storage. Allow repeat requests and report receipt, halt attempt and fresh confirmation separately. |
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
receipt. Arrival needs fresh matching station and stopped feedback.

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
