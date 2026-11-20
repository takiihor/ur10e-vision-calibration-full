# Operator app stage 1

The opt-in operator app provides Home at `/operator/#/` and AGV commissioning at `/operator/#/agv`. Calibration, Teaching and Pick/place keep their standalone pages and native module cores. The IPC owns hardware work, connection state and job admission. The browser uses the existing same-origin APIs and does not open a device connection.

Home reads `GET /api/state`, a protected, cache-only projection. Each item has a stable ID, typed value, source, acquisition time, freshness and quality. Expired values become unknown and retain their acquisition time for historical display. The AGV connection worker receives timed reports and polls when necessary. Both commissioning pages use its cache, with explicit port discovery and inquiry controls.

The commissioning page provides link setup, armed movement controls, learned station calls, alarms and a commissioning record. The record preserves the requested operator action, the wire command, the received status and operator observations. It identifies simulator or serial transport.

Use the locked Vue, PrimeVue and PrimeIcons dependencies and the adapted Sakai shell. Keep the third-party notices with the source and packaged assets. The Python server provides the authenticated entry point.

Line control, site mapping and scene integration have separate implementation scope. Stage 1 uses a standalone AGV simulator for offline commissioning and introduces no supplier endpoint.

See the [operator HCI guide](operator-page-hci.md) and [UI build instructions](../operator-ui/README.md).

## Scene and labels (Stage 2)

Open `/operator/#/scene` or `/operator/#/tests/scene` for the same display. The service still opens the legacy landing page by default. The Scene page links to the legacy tool and collision preview. The display has no motion controls and shares the operator shell's AGV Stop and Pause.

The service reads a local drawing and its display configuration at startup. It verifies the drawing's size and SHA-256, then serves the single configured, self-contained GLB at its protected, versioned URL. The browser revalidates cached drawing bytes through the authenticated endpoint. Scene uses drawing-frame metres and +Y up. The AGV marker is the last confirmed arrival on the current connection. Motion and its reported target are words. A stale report changes the live label to **Unknown** while the last confirmed station remains labelled as history. A new connection starts without that history.

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
JSON carries the manifest and all retained rows. CSV lists samples with observation and
acquisition times and quality. The Markdown summary names the transports, shows
counts and notes, and links to the full evidence.

Recorder data and downloaded files are local evidence and can contain entered
site details and raw device frames. Use the service data directory for private
storage. Recorder summaries identify selected line sources. Line samples keep the
original supplier timestamp and row ID in JSON and CSV. Line-test write,
receipt, readback and restore events belong to Stage 5.

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
then ages the sample on a monotonic clock. Repeated rows keep the same age
reference. At 3 s, values expire to Unknown. Partial rows show identifiable
values and make missing, invalid or failed tags unknown. If failure counts
cannot be reconciled to affected tags, all tag values are unavailable.
Reported counts are shown when supplied. Raw alarm bits carry no assumed
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
Stage 5 owns command approvals, simulator dispatch, cleanup, and the historical
API/operator archive. See [Line read setup](../operator-ui/README.md#line-reads).
