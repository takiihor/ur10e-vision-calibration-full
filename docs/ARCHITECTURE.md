# Operator app and vehicle executor

## Installation roles

The mains-powered controller runs the Dev console at `http://dev.localhost:8880/dev/`. It is
the operator control surface and owns recipe review, scheduling and
basket records. The built-in Calibration module runs test recipes. The planned
Main app loop will orchestrate normal production. The battery-powered
vehicle IPC owns hardware access and runs one accepted job at a time. The IPC keeps that job's instructions, checkpoints
and results. The controller keeps the recipe and the plan cursor.

| Installation role | Responsibility |
| --- | --- |
| `orchestrator` | Runs the operator UI and the Calibration module for test runs. Sends versioned jobs over the keyed IPC HTTPS API and tracks their results. |
| `executor` | Runs the stable IPC service with a stable data directory. Owns the wired AGV bus and checks admission and interlocks, journals work and holds on doubt. |
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
executor before retiring the bridge. Katana and developer modules reach the
same keyed IPC HTTPS API on the line LAN. The IPC allows one owner and one
executing job, disarms on owner change, and admits eligible jobs through its
existing interlocks. See [module API and keys](MODULE_JOBS.md).

The kiosk's listener serves cached state, including its age, without querying
the bus. Install the executor under a separate system service identity
so the graphical kiosk account cannot read its token or manage its service.

The local radio is an optional backup and starts disabled. Change transport
only after disarming, disconnecting and positively verifying that the former
owner has released the bus and cannot issue commands. Treat controller-local
radio as an explicit, mutually exclusive maintenance mode.

## Job contract

Protocol major 2 implements Operator check. During a rolling upgrade,
protocol 1 and 2 peers can display status. A control lease requires a
matching major version in the health and control envelopes. The `/v1/`
paths and per-job type versions remain stable. Use the scoped HTTPS
listener on the line LAN for developer modules.

| Operation | Required behaviour |
| --- | --- |
| Health and capabilities | Report protocol major, build, supported job types and versions, and executor boot identity. Keep cached status readable across incompatible builds and reject incompatible control. |
| Status | Report current and by-ID job state and result, last confirmed step, outstanding intent, device state and freshness, hold or recovery reason, and dispatch permission. |
| Connect, disconnect, arm, disarm | Require explicit control with fresh compatible state. Bind approvals to the executor boot, controller session and Stop generation. A new connection grants no motion authority. |
| Submit | Persist the run, basket, ordered steps, recipe and limits revision, job ID and immutable payload on the controller before sending. Persist acceptance on the IPC before execution. A repeated ID with the same canonical payload returns its existing state or result. A changed payload conflicts. A valid new ID submitted while occupied receives a recorded busy refusal. |
| Heartbeat | The Katana service defaults to 1 s heartbeats and a 3 s lease. An external module sends advancing heartbeats every 1 s. After 10 s without a valid renewal, the IPC latches Stop, holds, disarms, releases that owner and requires Operator check. Ignore duplicate, delayed and prior-session renewals. |
| Stop | Both hosts latch a new Stop generation independently of the normal request, device and journal locks. The IPC sends the halt frame on a motion-capable transport before any journal write. Katana shows receipt, halt attempt and confirmation separately. Only fresh confirmation from the current IPC boot for the applicable Stop may clear a pending Stop. |
| Physical commands | Start, Resume, Arm and station visits carry the boot, session and Stop generation reviewed by the operator. The IPC checks the latch before each physical step. Older authority expires even if the request arrives late. |
| Job history | The append-only IPC journal retains consumed job IDs across restarts. A known ID returns its recorded state. Before a physical effect, persist the intent. After observing the effect, record the evidence. |
| Read-only queries | Katana may repeat status and by-ID reads. An ambiguous physical submission enters Operator check immediately. Katana never resubmits it or advances the basket from a later reply alone. |

A station command acknowledgement establishes receipt. Fresh, stopped feedback
at the requested station establishes arrival. The last confirmed location,
the observed basket and the expected basket remain separate facts.

The AGV status field `max_stations` reports learned legs. A positive count
makes the start station 1 and allows calls through the count plus one, capped
at 255. A zero count allows no station calls. The operator UI, IPC admission
and wired travel checks use this same range.

A paired `dip` job identifies both step IDs, the source and destination,
tank and taught-endpoint references, expected basket state, time bounds,
and required final hold. The complete place, dwell, lift and transfer path
runs on the executor with simulated devices in Phase 1. Production dip
handling, live robot integration and taught-endpoint resolution are Phase 2.

## Operator check

When the outcome of physical work is in doubt, the IPC starts no new physical
action. An action already under way may halt or finish its current move into
a qualified hold. It records what was observed and disarms. A paired job
cannot start its next move under the expired authority.

Katana freezes the plan and shows **Operator check** with the reason, last
confirmed location, observed basket, job ID and recipe step. Missing or
conflicting evidence stays visible as such. The operator physically checks
the basket identity and location before choosing one of two outcomes.

- **Resume from step N** records the operator, observations and selected
  step. Katana obtains fresh status and a fresh arm, then issues a new job
  ID from that step under the basket's pinned recipe revision. Earlier
  measurements and the uncertain original job remain in the record. In the
  simulator the starting hold must be input or park and match the observed
  basket. Repeating a measured step adds a separate attempt.
- **Abort basket** records the operator, observations and outcome and flags
  the basket for inspection. Recording the choice does not unload the basket.

| Fault | Required outcome |
| --- | --- |
| Katana restarts during a basket | Recover the basket record and its pinned revision into Operator check, including at a boundary between jobs. Deliver any outstanding Stop before allowing positive controls. |
| IPC restarts, loses power or reports a different boot | The IPC boots disarmed. Katana retains the last confirmed observations, fences the old authority and requires Operator check. |
| Wi-Fi or lease loss | Katana freezes dispatch on doubt. The IPC independently holds and disarms on lease loss. An unavailable link cannot confirm a halt. |
| Low battery or stale device feedback | Refuse admission when reserve or interlock evidence is inadequate. During an active job, hold and disarm, show the observations, and require Operator check. |
| Stop during link loss | Show **Stop unconfirmed**. Deliver the pending Stop on reconnect before re-arm or dispatch. A physical e-stop remains the immediate site intervention during a partition. |
| Lost, partial or contradictory physical job reply, or mismatched basket, station or location | Hold and disarm. Preserve the original ID and measurements. Resume or Abort requires the operator's observations. |
| Power loss during publication or a transient journal write failure | Hold and disarm. When writes work again, require a physical operator check before release. Recover a pending Stop from its durable publication. |
| Missing, unreadable or altered retained file | Hold and disarm, preserve the remaining history and name the affected file with **call a technician**. |
| Measured dwell or air-time breach with intact evidence | Record the measured value, raise an alarm and set inspect. Continue under the existing physical interlocks. |

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

Record measured dwell, air time, PLC cycle evidence and equipment state for
each basket and tank. Count each rinse exposure once, including time in a
hold after link loss. On understay, overstay or excess air time, record the
measured value, raise the alarm and flag the basket for inspection. Continue
the reviewed programme when the next job meets its independent safety checks.
The operator retains the scrap decision. Stop remains available throughout.

## Phase acceptance and next phase

Phase 1 starts with an end-to-end 12-step simulated basket through the
existing UI and versioned job interface, including draft limits, paired
rinses, holds and recovered records. Exercise restart of either host,
lost replies and conflict detection, stale feedback, low battery, incompatible
builds and Stop during a partition. Keep real hardware checks to live
status, explicit arm and disarm after offline acceptance. The IPC service
name and data directory stay stable across version changes. Preserve prior
installation data and job evidence for migration or rollback.

Phase 2 puts real robot jobs, cameras and other vehicle devices
behind the IPC executor. The controller provides the live line integration.
Equipment sequencing, credential moves and production timing qualification
also belong to this phase. Require qualified local pose/interlock evidence
and measured stopping and travel times before live station visits. Require
cycle-specific local PLC evidence or an approved independent fallback before
live dips. Both require defined, observable holds and recovery responses for
power loss, a blocked destination and Stop during a partition.

### IPC tower light

The executor chooses one display pattern from cached AGV feedback, dispatch,
Arm authority, Stop latch and recovery state. Both the touchscreen and the
tower light use that pattern. Fresh feedback can show travel even when the
executor has no job. A report of motion, positive speed, or a target that
differs from the current station starts the motion indication. Stale or
disconnected feedback cannot show green. A detached screen shows the
neutral status and the tower light goes off unless an alarm or recovery
indication applies.

The graphical session's user service watches DRM for the single enabled
HDMI-A-1 touchscreen. It supervises a separate GTK/WebKit viewer using
XWayland window hints to reject focus and stay behind developer windows.
The viewer reads only the loopback display, with an ephemeral browser
context. SSH sessions and developer processes are outside the unit.

The light's dedicated display thread owns the ECB-306 USB CDC port.
It sends 115200 baud, 8N1 output masks as
`GW` followed by two uppercase hex digits and carriage return. The board sends
no acknowledgement. The driver updates all four tower channels with one
all-output frame. The ECB-306 output bank is dedicated to this light.

At startup and on reopen the display thread sends `GR\r` and expects `GR`, two
hex digits and carriage return within the serial read timeout. A missing or
invalid reply or a failed write marks the light unavailable and triggers a
retry independently of the AGV. Stop and travel admission have no
dependency on it. The worker clears the outputs during clean shutdown. Install
`tools/anodizing-executor-tower-light.conf` as a systemd drop-in for the executor
so `ExecStopPost` attempts the same clear after a crash. The clear needs the IPC
and ECB-306 to be powered and communicating. The
[manual](MANUAL.md#tower-light) covers configuration and the attended lamp test.

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
label with `ANODIZING_EXECUTOR_BUILD` and give the service its key hashes
through `ANODIZING_EXECUTOR_TOKEN_FILE`. The keyed HTTPS listener binds the
configured line address. The local IPC control listener and the separate
cached kiosk display listener bind loopback. A display-only
installation can read that display port using `--status-display-port`
as its source and `--port` as its own listener.

Set `ANODIZING_EXECUTOR_PORT` to the IPC HTTPS port,
`ANODIZING_EXECUTOR_CERT_FILE` to the pinned IPC certificate and
`ANODIZING_EXECUTOR_TOKEN_FILE` to the private Katana service key. Set
`line_network.ipc_address` in the private site profile. The Katana service
sends the lease heartbeat independently of the browser. Its existing AGV page uses the backed-by-executor Wired
link. In commissioning the executor accepts Connect, a status query,
software Arm, Disarm and Disconnect. Arm checks fresh stationary status
and writes no command. Live station visits and dips remain fenced.
Software Stop can latch even when disarmed. The commissioning transport
cannot attempt a wire halt. Use the physical e-stop for intervention.

### Katana line-link alert

In Katana's private `site-profile.json`, set `executor_link.route_address`
to the IPC's line-side IPv4 address used by the HTTPS gateway. Katana reads
the matching interface's carrier or operational state from Linux and samples
the existing cached executor reachability about every five seconds. If the
adapter state is unavailable, the diagnosis uses executor reachability.
The diagnosis uses cached state without querying devices.

The shared `GET /api/status` response includes `line_link` for Katana. A red
banner shows when the outage was detected and what to check. The outage time
stays fixed when the diagnosis changes. A green banner shows when the link
returns and points to the AGV page for about 30 seconds.

### Katana AGV and Bluetooth refusal feedback

Katana keeps the original error in AGV and Bluetooth action responses and
adds `code`, `message` and `hint`. The operator UI names the attempted
action and displays the reason and next step. Katana translates bare
executor strings at the HTTP boundary. New and unknown reasons remain
visible with technician guidance. Cached status provides the same
feedback for active blocks. Executor admission and the Stop path retain
their existing authority.

Both pages use the most recent wired report already observed by Katana
for the learned leg count, current station and read time. A disconnected
or old report is marked last known. The AGV page shows the callable range
and disables higher station buttons with the reason visible next to the
buttons. Raw Bluetooth route records use a separate read path and do not
supply this wired count or current position.

### Supervised manual station calls

The AGV page accepts a separate, time-limited manual station permission. The
IPC installation sets `ANODIZING_MANUAL_TRAVEL_UNTIL` to an absolute Unix UTC
timestamp. Omit it to keep live station calls disabled. For a supervised test,
set the deadline three hours from activation. Restarting the service with the
same timestamp preserves that deadline. The editable DRAFT battery setting is
`ANODIZING_MANUAL_TRAVEL_MIN_BATTERY` in percent, initially 20.

Before each call the operator selects the destination and confirms “I am at
the e-stop, the arm is folded, and the path and tank equipment are clear.”
Katana records the confirmation time and destination with the job. The IPC
checks the request against the current lease, boot, Stop generation, fresh
fault-free status, battery and learned station range. The raw `max_stations` counts learned
legs. Zero permits no calls, one permits stations 1 and 2, and station
numbers are capped at 255. The AGV page keeps the software Stop accessible
during confirmation. The physical e-stop is the emergency stop.

The supervised line tape runs beside the tanks except for the dummy tank.
The disconnected UR10e stays folded during travel. This installation uses
`ANODIZING_MANUAL_TRAVEL_ISOLATED_TRACK=1` alongside the existing deadline.
Tank-clear and equipment-off signals are not connected yet. Before each
call, check the path and tank equipment by eye and confirm the arm is folded.
The IPC uses fresh AGV battery percentage for reserve and rechecks the
confirmation, its destination, the deadline and the latest vehicle report
at the station write. During travel a lost interlock or expired window
requests a halt and requires Operator check if the outcome is uncertain.
No robot pose samples are synthesized.

The isolated track defaults to blocking module station visits and raw
motion. With `ANODIZING_MODULE_TRAVEL_ON_ATTESTED_TRACK=1`, the current
owner's Arm confirmation supplies the folded-arm, clear-path and
clear-equipment checks. The IPC requires an open window and fresh battery
above the floor. At the final send it rechecks the owner, lease, Stop
generation and Arm confirmation.

The ordinary wired manual installation still requires qualified robot pose,
reserve and clear tank/equipment feedback. Neither installation grants
programme travel, recipe travel or live dips.

The shipped production role uses only the wired driver. The backup serial
radio requires a deliberate maintenance changeover to the existing serial
mode on the controller. Disarm, disconnect and verify physical isolation
of the executor before disabling its configured connection on Katana
and enabling local serial support. Restore the
executor path only after verified radio isolation.

Katana's Bluetooth maintenance tab reads controller settings and learned route
records using the local adapter. Bluetooth operations use the enrolled Katana
operator session. Developer modules use the job gateway. It fences wired Connect and Arm while the
Bluetooth session is open. The wired executor must report disconnected,
disarmed and idle before a Bluetooth write. Settings are snapshotted and
verified by read-back. The verified route clear requires a fresh review and
checks both post-clear counts. Bluetooth motion is unavailable until its
Stop behavior is qualified.

Set `ANODIZING_PROGRAMME_FILE` to a private JSON list of ordered steps
with `id`, `tank`, `role`, `station` and positive `setpoint` in seconds.
The existing Pick/place screen exposes the Calibration module's simulated
basket programme. For two-process simulation, follow the
[fake HTTPS gateway setup](MODULE_JOBS.md#local-fake-instance). The AGV page
and Calibration module use separate client keys and share one executor owner.
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

The IPC appends numbered records in `.executor-journal/` and publishes the
entry count and digest in `.executor-journal-tip.json`. Reopening an ID
returns its recorded state. Install each Phase 1 release with fresh executor
and programme state. Keep the prior release's complete evidence read-only
outside the new state directory. Preserve site configuration and independently
provisioned setpoints. Close an active basket on the owning release through
Operator check before switching readers. An operator who resumes can finish
on that release. The installer rechecks the frozen state before archiving.
A same-release restart reopens the existing journals.

A power loss can occur after an effect and before its confirmation reaches storage.
Retain both the intent and the last confirmed observation. A completed
dwell requires measured immersion, qualified PLC completion and a confirmed
lift out of that tank on the same boot.

At the first station job the fake executor verifies the input handoff,
binds the basket to the run and checks that the first dip starts at input.
An independently verified output hold for a previous run allows the
simulated unload and next input load, including when two successive baskets
have the same label. A different basket at input or an unverified hold
blocks the load before station travel.

Start at the reviewed input, inspect the effective draft revision, and
start the simulated run. The run's revision and result history are durable
in the orchestrator data directory. Use Operator check for
uncertain execution. A quality flag stays with the basket through completion.
The operator can reopen an earlier basket by its run ID in the record selector.
Stop changes dispatch permission independently of the basket outcome.
The urgent area distinguishes receipt, halt attempt and confirmed halt.
Katana latches Stop in memory and sends it before writing its witness. After
the HTTP body is sent, or the send attempt fails, Katana saves the pending
ID so restart can deliver it again. The send and the disk commit are separate
operations. A power cut between them can leave the IPC as the only witness.
Each press of Programme Stop creates a fresh physical ticket and bypasses
operator-action deduplication. The Stop button stays available while Start
is awaiting its reply. On a motion-capable
wired transport, the halt writer shares frame serialization with ordinary
writers and can transmit during a blocked status read. The IPC checks for a
Stop again at the point where each physical effect would start.

To confirm Stop, Katana checks the executor boot, disarm, absence of an active
or unfinished job, and fresh stationary feedback sampled after the applicable
halt was sent on that same IPC clock. On a serial link the first bytes of a
status frame determine its sample time. A status-only transport that cannot
issue a halt leaves Stop unconfirmed. A later successfully halted ticket
on the current boot can cover an earlier failed attempt. Replies from a
retired IPC boot cannot clear the pending ticket. Contradictory status puts the ticket
back into delivery. Once a successful Arm clears the current Stop, the prior
confirmation appears as history.

Manual recovery names a station only with fresh stopped arrival feedback.
After a confirmed halt between stations, the operator can record the
observed location and Abort. Further travel requires a qualified endpoint.
A transient basket-receipt write failure keeps the worker alive to retry
storage recovery before allowing an operator decision.

Actual dwell is the interval between confirmed immersion and lift on
one executor boot. The record keeps the PLC cycle, elapsed time, exact
simulated done edge and sample time. For anodize, overstay runs from the
qualified done edge to lift. A late sample therefore cannot hide either
immersion or post-done time.

The fake executor persists device state separately from the job journal.
Missing or invalid stored plant evidence leaves the simulator fenced
with the filename and technician instruction. A default device snapshot
cannot establish the location of a basket already in progress.

The snapshot's `reserve_s` field records usable simulated autonomy in
seconds. Unknown or stale readings block admission. An admitted job needs
the configured fake travel, dwell and transfer durations plus a synthetic
10-second recovery margin. The simulator consumes those durations and
records the available and required reserve at admission and at each stage.
A depleted supply stops dispatch and retains the earlier confirmed stages,
including on executor restart. These values do not establish a physical
battery budget or brownout response.
At Operator check, Katana looks up the original job ID and checks the IPC's
measurements against the pending request before recording them. Basket, run,
station, transfers and final hold must agree. A terminal success must have
finished every physical intent. Reading the result can add a verified row or
outcome to the basket record. If the job details still disagree at decision
time, the record retains the original job ID and the reason that evidence is
outstanding. Reopening that basket can attach later verified evidence.
The operator chooses the next programme step.

Katana preserves measured dwell and air-time evidence from before a hold.
An operator observation records who checked the basket, where and which step
they selected. It supplies no measured immersion, lift or PLC timestamps.
Earlier job outcomes, measurements and inspection flags remain part of
the basket history.

At each programme station leg the IPC checks the current local travel interlocks.
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
