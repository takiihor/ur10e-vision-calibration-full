# Operator app stage 1

The opt-in operator app provides Home at `/operator/#/` and AGV commissioning at `/operator/#/agv`. Calibration, Teaching and Pick/place keep their standalone pages and native module cores. The IPC owns hardware work, connection state and job admission. The browser uses the existing same-origin APIs and does not open a device connection.

Home reads `GET /api/state`, a protected, cache-only projection. Each item has a stable ID, typed value, source, acquisition time, freshness and quality. Expired values become unknown and retain their acquisition time for historical display. The AGV connection worker receives timed reports and polls when necessary. Both commissioning pages use its cache, with explicit port discovery and inquiry controls.

The commissioning page provides link setup, armed movement controls, learned station calls, alarms and a commissioning record. The record preserves the requested operator action, the wire command, the received status and operator observations. It identifies simulator or serial transport.

Use the locked Vue, PrimeVue and PrimeIcons dependencies and the adapted Sakai shell. Keep the third-party notices with the source and packaged assets. The Python server provides the authenticated entry point.

Line control, site mapping and scene integration have separate implementation scope. Stage 1 uses a standalone AGV simulator for offline commissioning and introduces no supplier endpoint.

See the [operator HCI guide](operator-page-hci.md) and [UI build instructions](../operator-ui/README.md).

## Scene and labels (Stage 2)

Open `/operator/#/scene` or `/operator/#/tests/scene` for the same display. The service still opens the legacy landing page by default. The Scene page links to the legacy tool and collision preview. The display has no motion controls and shares the operator shell's AGV Stop and Pause.

The service reads a local drawing and its display configuration at startup. It verifies the drawing's size and SHA-256, then serves the single configured, self-contained GLB at its protected, versioned URL. The browser revalidates cached drawing bytes through the authenticated endpoint. Scene uses drawing-frame metres and +Y up. The AGV marker is the last confirmed arrival on the current connection. The map and text list label that historical placement on the AGV and mounted robot, including when an AGV stops between stations. Motion and its reported target are words. A stale report changes the live label to **Unknown** while the last confirmed station remains labelled as history. A new connection starts without that history.

The renderer uses the service's selected robot model and the existing UR10e joint transforms. The browser compares the downloaded model SHA-256 with the joint sample's model identity before drawing it. The transform maps robot +Z to drawing +Y, applies the entered mount offset and rotation, then places it at the last confirmed AGV station in the entered track frame. The track frame uses +X from start to end, projected horizontally, and +Y up. The position and heading are a fixed display reference. The AGV supplies no continuous pose.

Opt-in scene simulation produces six fake joint angles at 5 Hz and optional scripted tank statuses at 1 Hz. A job takes precedence over the fake joint stream. Scene only consumes feedback the Manual or Teacher worker has already cached for that job. It shows **Unknown** on contention, a missing or expired sample, or an invalid model. Live idle RTDE is deferred pending stalled connect/read/disconnect checks during job admission, Stop and travel reads. Existing native stillness and motion gates remain independent of display data.

Scene reads the optional canonical `tank` from an explicitly selected saved Teaching project. Older projects show **Tank not taught**. Tank authoring and capture binding belong to Teaching. Tank status uses the line reader’s mapped basket feedback when configured. Scene-only simulation still accepts scripted status. The text list carries the same labels and ages as the map and remains available without WebGL. See the [local Scene configuration](../operator-ui/README.md#local-scene-configuration).

## Recorder (Stage 3)

The operator and test routes, `/operator/#/records` and `/operator/#/tests/records`,
share one server-owned recording. The legacy landing page and AGV commissioning
remain available. Recorder links to the AGV checklist and its two commissioning
exports. That checklist, operator observations and link-quality summary use the
same browser session as legacy commissioning.

Start with a selection of stable variable IDs and 0.2, 1 or 5 Hz. The worker and
`GET /api/state` use the same AGV, Scene and line projections. Reads
use existing acquisitions and keep their source time. A missing or expired
value is null with its quality and reason. Recording does not enumerate ports,
poll devices, start jobs or acquire an idle robot session. The available variable
catalogue grows as the later modules are ported.

The loopback, token-protected `/api/records` endpoint lists sessions and accepts
Start and ID-bound Stop. Closing the browser leaves the worker running. The
server writes a manifest and append-only sample and event rows under
`<data-dir>/records/<id>/`. The manifest records the selected variables and
rate, optional session details and notes, starting AGV connection settings and
transport, and whether Scene simulation was enabled. The start time and AGV
context are captured together. The server buffers events from that point while
it writes the manifest. Event records pair the AGV connection ID with TX IDs
and attempts, log raw TX/RX and alarms, and pair accepted jobs with their
request IDs and outcomes. Halt entries include the requested command and the
wire command. Repeated requests that join a pending delivery refer to its
original TX ID. Job audit files stay in the native store.

The first cap reached, 2 hours or 50 MiB including the manifest, ends the
session. An event buffer fills only when the recorder cannot keep up. It ends
the session visibly without blocking native producers on recorder storage.
A service restart retains complete rows and marks an unfinished session
Interrupted. The Recorder lists a damaged session by ID as unreadable and
preserves its files. Healthy finished sessions remain available for download.
JSON carries the manifest and all retained rows. CSV lists samples with observation
and acquisition times and quality. The Markdown summary names the transports, shows
counts and notes, and links to the full evidence.

Recorder data and downloaded files are local evidence and can contain entered
site details and raw device frames. Use the service data directory for private
storage. Recorder summaries identify selected line sources. Line samples keep the
original supplier timestamp and row ID in JSON and CSV. The recorder links
simulator line-test dispatch, feedback and restore events by test and request ID.

## Line reads (Stage 4)

The operator's overview, tank monitor, alarms and browser are at `/operator/#/line`.
`/operator/#/tests/line-api` uses the same view and adds writable readbacks to the
catalog filters. Both views also show metadata, mapping warnings and recent
read diagnostics. Bookmark a subview such as `#/line/tank`. The legacy console
still runs separately. The service continues opening the legacy landing page.

One optional service worker loads the catalog and polls at up to 1 Hz.
Startup retries the catalog while it is unavailable. An authenticated
refresh requests a catalog update from that worker. Read requests from browser
tabs and Recorder copy the cached data. The worker sends only list and read
GETs to one configured origin. It never follows redirects. The service key
stays at the service, including in simulation and recording paths.

HTTP reachability and sample quality are separate. A fresh sample needs two
advancing rows after startup, a changed catalog, clock reset, lost contact or
expiry. The service retains the unmodified supplier timestamp, row ID and
receipt time. It computes initial age from the configured clock correction and
then ages the sample on a monotonic clock. The overview shows the applied clock
correction and corrected timestamp alongside the raw supplier timestamp. The
browser includes elapsed request time when deciding expiry. Repeated rows keep
their age reference. At 3 s, values expire to Unknown. Partial rows show valid
tags when the reported counts agree with the catalog and identifiable failures,
or when the supplier omits both counts. A count contradiction suppresses all tag
values. Omitted counts display as Unknown. Communication diagnostics identify
the row received on each individual request. Raw alarm bits carry no assumed
polarity. Published engineering values are kept at their reported scale.

The local line profile assigns catalog tags to physical tanks. When a door
belongs to a different physical tank than the name implies, assign the reported
door tags only to the confirmed physical tank. The monitor names the original
tag and flags the alias. Duplicate bindings, shared addresses, and reviewed
tag identities that have drifted stay out of the physical view. The raw
catalog and the Recorder still identify each by the supplier's tag name.
Auxiliary tag groups remain browseable. Scene uses the exact same basket
variable and age as the tank monitor. The drawing and Teaching assignment
supply display identity only.

The standalone modules and native job paths require no active line source.
See [Line read setup](../operator-ui/README.md#line-reads).

## Unified line test (Stage 5)

The test route adds the writable Boolean checklist, reviewed command and
feedback cases, one-shot approval, and separate API and human evidence to the
shared read views. The simulator is the only write-capable client. Its
transport has no network write method. Enabling the startup flag with live
reads leaves writes disabled. The operator route exposes the same read cache
and catalog diagnostics. The legacy service landing and independently
launched console remain available.

The server grants a session for up to 30 minutes. One approval lasts 30
seconds and binds the session, request ID, simulator instance, reviewed
case, observation limit, and current tag metadata. Before writing, the
line worker refreshes the catalog, checks the complete fresh row, both
mode bits and the reviewed zero-command and feedback baselines. An AGV
connection or concurrent job prevents admission. Once reserved, the test
fences conflicting operations and saves intent under the private data lock
before sending 1. The worker tracks a five-second page lease so a lost page
requests cleanup independently of future browser requests.

The worker serializes the observation and same-command zero dispatch with
Stop, timeout, service close, and lease expiry. Cleanup refreshes the
catalog and checks both mode bits and freshness again. The worker requires
matching receipts and a PLC row strictly later than the baseline and
acceptance for feedback and zero confirmation. A provisional match is
recorded as **Observed**. The eight-second recovery window can wait for
the gates to return. If evidence or ordering remains uncertain, or audit
storage fails, the alarm and fence remain in force. Unresolved or unreadable
intent also fences admission on restart. The server never replays 1.
Power-loss response, reconciliation and safe early clear require site review
before any real write implementation.

Simulator history keeps baselines, receipts, readbacks, API disposition,
separate time-stamped human judgments and original notes. Exports include a
summary-first Markdown report, CSV and complete JSON. Historical imports
preserve the console originals under a private content digest. The view
keeps archive context, recorded identities, latest judgments, automated
results, human backfills and explicit API overrides. Historical results
grant no write authority. See [setup and import](../operator-ui/README.md#unified-line-test).

## Pick/place and Recipe (Stage 6)

The Pick/place menu opens `/operator/#/pick-place`. The legacy
`/pick-place` page and service landing remain available. The test route
`#/tests/pick-place` uses the same page. The Robot steps, MCU and Tool
power test aliases open their corresponding control groups in the same layout.

The page reads each explicitly selected Teaching project to display its
taught tank and station. Missing tank metadata displays **Tank not taught**.
Station fields can be edited and flag a disagreement with the taught value.
Tank metadata gives no movement permission. Changing recipe inputs clears
the review and both approvals, including while the read-only review job is
pending. Run uses a fresh request ID and the digest from the completed
review. The native service reopens and compares the saved inputs before
motion. A cross-station run requires the existing launch flag and separate
confirmation of AGV Manual mode, clear route and physical e-stop coverage.

The Vue page reads the connection worker's cached status at the recipe's
three-second freshness limit. Opening or polling the page leaves AGV and
robot devices to their existing owners. A saved travel-pose read is passive.
**Check live travel pose** explicitly requests the existing guarded read.
A parked recipe binds the approvals to the connection and halt generation.
AGV Stop, Pause or link closure clears them. The shared bar keeps AGV Stop and Pause
separate from job-bound Pick/place Stop. Escape targets AGV Stop here.
Accepted automatic work can continue in the service after a page disconnect.

Single PICK/PLACE and Robot steps use the server-held plan and phase IDs.
The operator confirms each shown prompt separately. An MCU child stays
bound to its parent's grip or release prompt. A robot Stop clears the
visible approval and disables confirmation for that job. Evidence shows
terminal results, commanded transfer counts, ACKs, arrivals and an unknown
item after interruption. The service retains execution fences across
reload. Browser reload may read a saved request ID or follow the current
job. It does not restore confirmations, answer prompts or submit motion.

MCU sensor replies and tool-power reads are historical, timestamped
evidence. Check request reads the saved job. If a tool submission loses
its acknowledgement, the page can explicitly retry the same ID with its
exact payload. Tool off requires the Released declaration. An ambiguous
motion submission stays fenced in the browser until the request ID can
be inspected. Native owner, TCP, model, swept-path, AGV and device checks
remain authoritative for all dispatches.

See [Pick/place routes and operation](../operator-ui/README.md#pickplace-and-recipe).

## Teaching and snapshots (Stage 7)

Both Vue Teaching routes use the existing Teacher owner, bounded camera
worker, native validation, return and manual descent contracts. The
legacy service entry remains the default. The protected read-only project
lookup can include saved evidence with `evidence=1`. Reading a project
opens no camera or robot receiver. The browser stores the selected project
and request ID for inspection. Path and AGV approvals require new input
after a reload. The browser sends a heartbeat while supervising its own
checked move on the visible Teaching page. The service owns heartbeat
expiry and job-bound Stop, including the handoff before the native
motion worker starts. The manual descent uses its own operation ID and
existing exact prompt.

The project retains schema version 1. An omitted tank preserves the
saved field. A validated canonical ID replaces the field, and an explicit
null clears it. Capture stages the reference observation before publishing
the project manifest with the station, tank and captured flag. A serial
station uses the same qualified arrival, connection and generation
throughout acquisition, including a confirming report after the camera
worker completes. A changed, stale or missing required report refuses
publication. Manual stations keep their separate operator provenance.

Each snapshot kind has metadata for the JPEG digest, camera and provider,
frame receipt time, detection outcome, and capture ID. The worker
retains the same image for a given ID. The token-protected snapshot
endpoint verifies project containment, identity format, JPEG size and
digest. Operator UI labels distinguish the current evidence from dated
history. Capturing a validation image creates a separate native validation
identity and timing window. Saved validation history can display its bound
image after setup or service restart. Saved evidence supplies no restored
movement approval. See the [Teaching workspace instructions](../operator-ui/README.md#teaching-and-snapshots).

## Calibration (Stage 8)

The Calibration and test routes render one Vue workspace. Camera access,
pendant collection, and automatic planning and collect/replay keep their
independent IPC gates. The legacy route and the native module entry points
remain available.

Existing calibration jobs continue to own the camera and robot interfaces.
Vue submits through the token client and inspects the persisted job ID.
The registered capture Stop target accepts only the current calibration job
ID. Stop invalidates the current prompt and latches before the native runner
starts. A started worker receives the existing interrupt signal and retains
service ownership through cleanup. The page reports the request independently
from the physical outcome. Active targets remain reachable from other Vue
routes, and Escape keeps the AGV Stop binding.

Camera-only results and worker observation events bind image bytes and
capture metadata in the same job record. Time is taken upon frame receipt.
Multi-frame pose estimation and quality checks are labelled as burst
results. Historical images without capture metadata remain viewable.
Calibration artifacts and verification reports remain in their respective
sessions. Teaching handoff checks a completed candidate against the current
selected path and content digest. Old jobs without a stored digest require
the file to match the complete saved result at handoff.

The tool editor retains the vendor scene validation, model hash and file
workflows. Its same-origin bridge accepts messages only from the embedded
preview and hands back the saved scene. A saved or displayed scene never
replaces the native checks on automatic admission and prompt approval.
See [Calibration setup](../operator-ui/README.md#calibration).
