# Operator app stage 1

The opt-in operator app provides Home at `/operator/#/` and AGV commissioning at `/operator/#/agv`. Calibration, Teaching and Pick/place keep their standalone pages and native module cores. The IPC owns hardware work, connection state and job admission. The browser uses the existing same-origin APIs and does not open a device connection.

Home reads `GET /api/state`, a protected, cache-only projection. Each item has a stable ID, typed value, source, acquisition time, freshness and quality. Expired values become unknown and retain their acquisition time for historical display. The AGV connection worker receives timed reports and polls when necessary. Both commissioning pages use its cache, with explicit port discovery and inquiry controls.

The commissioning page provides link setup, armed movement controls, learned station calls, alarms and a commissioning record. The record preserves the requested operator action, the wire command, the received status and operator observations. It identifies simulator or serial transport.

Use the locked Vue, PrimeVue and PrimeIcons dependencies and the adapted Sakai shell. Keep the third-party notices with the source and packaged assets. The Python server provides the authenticated entry point.

Line control, site mapping and scene integration have separate implementation scope. Stage 1 uses a standalone AGV simulator for offline commissioning and introduces no supplier endpoint.

See the [operator HCI guide](operator-page-hci.md) and [UI build instructions](../operator-ui/README.md).
