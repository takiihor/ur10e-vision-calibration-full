# Operator app stage 1

The opt-in operator app provides Home at `/operator/#/` and AGV commissioning at `/operator/#/agv`. Calibration, Teaching and Pick/place keep their standalone pages and native module cores. The IPC owns hardware work, connection state and job admission. The browser uses the existing same-origin APIs and does not open a device connection.

Home reads `GET /api/state`, a protected, cache-only projection. Each item has a stable ID, typed value, source, acquisition time, freshness and quality. Expired values become unknown and retain their acquisition time for historical display. The AGV connection worker receives timed reports and polls when necessary. Both commissioning pages use its cache, with explicit port discovery and inquiry controls.

The commissioning page provides link setup, armed movement controls, learned station calls, alarms and a commissioning record. The record preserves the requested operator action, the wire command, the received status and operator observations. It identifies simulator or serial transport.

Use the locked Vue, PrimeVue and PrimeIcons dependencies and the adapted Sakai shell. Keep the third-party notices with the source and packaged assets. The Python server provides the authenticated entry point.

Line control, site mapping and scene integration have separate implementation scope. Stage 1 uses a standalone AGV simulator for offline commissioning and introduces no supplier endpoint.

See the [operator HCI guide](operator-page-hci.md) and [UI build instructions](../operator-ui/README.md).

## Scene and labels (Stage 2)

Open `/operator/#/scene` or `/operator/#/tests/scene` for the same display. The service still opens the legacy landing page by default. The Scene page links to the legacy tool and collision preview. The display has no motion controls and shares the operator shell's AGV Stop and Pause.

The service reads a local drawing and its display configuration at startup. It verifies the drawing's size and SHA-256, then serves the single configured, self-contained GLB at its protected, versioned URL. Scene uses drawing-frame metres and +Y up. The AGV marker is the last confirmed arrival on the current connection. Motion and its reported target are words. A stale report changes the live label to **Unknown** while the last confirmed station remains labelled as history. A new connection starts without that history.

The renderer uses the service's selected robot model and the existing UR10e joint transforms. The browser compares the downloaded model SHA-256 with the joint sample's model identity before drawing it. The transform maps robot +Z to drawing +Y, applies the entered mount offset and rotation, then places it at the last confirmed AGV station in the entered track frame. The track frame uses +X from start to end, projected horizontally, and +Y up. The position and heading are a fixed display reference. The AGV supplies no continuous pose.

Opt-in scene simulation produces six fake joint angles at 5 Hz and optional scripted tank statuses at 1 Hz. A job takes precedence over the fake joint stream. Scene only consumes feedback the Manual or Teacher worker has already cached for that job. It shows **Unknown** on contention, a missing or expired sample, or an invalid model. Live idle RTDE is deferred pending stalled connect/read/disconnect checks during job admission, Stop and travel reads. Existing native stillness and motion gates remain independent of display data.

Scene reads the optional canonical `tank` from an explicitly selected saved Teaching project. Older projects show **Tank not taught**. Tank authoring and capture binding belong to Teaching. Tank status inputs are scripted or **Unknown** until the line reader is integrated. The text list carries the same labels and ages as the map and remains available without WebGL. See the [local Scene configuration](../operator-ui/README.md#local-scene-configuration).
