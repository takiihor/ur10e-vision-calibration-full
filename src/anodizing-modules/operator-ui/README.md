# Operator app

Open `/operator/` on the local Anodizing Modules service. Home reports service and AGV state. The AGV link opens commissioning. The sidebar lists Calibration, Teaching, AGV and Pick/place in that order. Its Legacy labels identify the links to the standalone Calibration, Teaching and Pick/place pages. The original landing page and AGV link are the defaults.

## Build and package

From this directory on a development machine with Node 22 and npm:

```bash
npm ci --offline --cache /path/to/prepared/npm-cache --ignore-scripts --no-audit --no-fund
npm test -- --maxWorkers=2
npm run build
```

The exact dependency versions and integrity hashes are in `package-lock.json`. The locally adapted Sakai 5.0.0 layout supplies the topbar, collapsible sidebar and content area. The layout uses local CSS, system fonts and bundled PrimeIcons. PrimeVue uses the Nord dark preset in `nord.js`, built on Aura. It supplies the CSS colour tokens for both routes. Connect and Query status use Frost. Stop and critical alerts use Aurora red. State colours have a matching icon and label. The contrast test checks the emitted tokens for 4.5:1 text and 3:1 control boundaries and focus rings, including PrimeVue fields, overlays, states and data views. The bundle carries the Sakai and dependency notices. [Nord palette](https://www.nordtheme.com/docs/colors-and-palettes/) · [Nord MIT licence](https://github.com/nordtheme/nord/blob/develop/license)

Vite writes `../app/anodizing_web/operator_dist/`, including the hashed assets and HTTP allowlist. Commit the generated output with the UI changes. Build the Python wheel from a clean app source directory with the generated output present. The Python package includes the entry, asset allowlist, fonts, CSS, JavaScript and notices. The IPC only needs the Python package and its usual dependencies. For local iteration, use `npm run build -- --watch` with the normal Python service in simulation.

## State and controls

The app sends the injected `X-Module-Token` header on same-origin API requests. The entry is served fresh with a per-response PrimeVue style nonce. A service restart requires a reload and a deliberate new connection. AGV status reads use `/api/state` and `/api/agv?cached=1`. `/api/agv?ports=1` refreshes the port list and command capabilities. AGV operations use the existing `POST /api/agv`.

The commissioning view uses the same connection, checklist, notes and record storage keys as the legacy AGV page. Open the AGV route to use the simulator, then choose **Simulator** and **Connect**. The fixed StopBar stays above the Sakai sidebar and overlays. Escape requests one AGV software Stop. Movement requests carry the current server Stop generation. The UI follows the returned urgent command result and reports stopped only when the server confirms zero speed. Robot stops for active Manual and Teacher sessions display their own buttons and use their own bound job IDs. Opening the AGV route on an armed link requests disarm. Inspect the link before issuing any new request after a failure.

The AGV reports learned legs. Station calls include the start as station 1, up to station 255. A report of zero learned legs disables station calls. The simulator input counts physical stations, so setting it to two produces a report of one learned leg.

The Sakai follow-up was verified with fake ports, software tests and the in-process simulator. Field serial, radio timing, physical e-stop and site acceptance require hardware verification.

## Local Scene configuration

The Scene and Scene test routes share one display. Open `/operator/#/scene` or `/operator/#/tests/scene`. The drawing, geometry, tank names and status mapping belong in the service's private data directory. The sample below is synthetic.

Add a `scene` section to `<data-dir>/site-profile.json` and restart the service after changes.

```json
{
  "scene": {
    "drawing": { "path": "local-drawing.glb", "sha256": "<64 lowercase hex digits>" },
    "track": { "start_m": [0, 0, 0], "end_m": [4, 0, 0], "stations_m": { "1": 0, "2": 4 } },
    "robot_mount": { "xyz_m": [0, 0.35, 0], "rpy_rad": [0, 0, 0] },
    "tanks": [
      { "id": "Tank01", "anchor_m": [1, 1, 1], "status_key": "FixtureStatus" }
    ]
  }
}
```

- Supply a self-contained GLB 2.0 under 12 MB with metre units, the drawing origin and +Y up. Keep it under the data directory and record its SHA-256 after export. An invalid drawing hides the placement and leaves the label list available.
- Enter endpoints in drawing metres. Station keys are canonical integers from 1 to 255. Distances are distinct metres from the start along the track. Enter the robot mount in AGV display coordinates with +X along the entered track and +Y up. Give roll, pitch and yaw in radians. The display uses the entered reference for the mount and labels the geometry as entered, not measured.
- Enter one anchor for each tank, even when several tanks share a mesh. IDs use `Tank01` through `Tank99`. An optional local `label` overrides the numbered title. An optional `status_key` names the future line variable, without the `line.` prefix. Use the same key only when both labels describe the same reported signal.
- To exercise the stage offline, start the launcher with `--simulate-scene` and all hardware flags off. Add `"scripted_line": { "FixtureStatus": "Occupied" }` inside `scene` for a synthetic status. Accepted fixture words are Occupied, Empty, Open, Closed, Active and Inactive. Choose the AGV simulator separately when a station label is needed.
- Enter the saved Teaching project directory relative to the data directory and choose **Load project**. Only the selected project's saved tank ID marks a label. The local GLB and project are served at runtime and stay out of the bundle.

The Scene's lazy WebGL chunk uses the same pinned three.js version as the legacy robot preview. Bring the three.js package in the prepared offline npm cache when building. Legacy tool, camera and collision preview workflows remain at `/preview/tool-envelope.html`.

## Recorder

Open `/operator/#/records` (or `/operator/#/tests/records` for the same panel).
Choose variables and a sample rate, add optional session details, then select
**Start recording**. You can navigate away or close the tab. Return to Recorder
and select **Stop recording** to finish and download the Markdown summary,
CSV samples or full JSON. The AGV checklist and its commissioning exports are
linked from Recorder.

The service saves sessions under `<data-dir>/records/`. Keep the directory and
downloads local because they contain operator notes and device evidence. Local
simulation uses the existing AGV simulator and optional Scene fixtures.

## Line reads

Open `/operator/#/line` for the read-only operator display or
`/operator/#/tests/line-api` to browse all catalog tags, including writable
readbacks. Use Line view to open Tank monitor, Signal browser, Alarm signals,
Tag catalog or Communication log. Signal and catalog search accepts names,
descriptions and addresses. The catalog's Advanced metadata option shows the
address, type, multiplier and published limits. Refresh catalog asks the shared
worker to reload its inventory. Download log JSON or CSV saves the most recent 100 read events with the
source mode. The legacy console keeps its existing
independent launch workflow, and `/` remains this service's legacy landing page.

Line reads default to disabled. Set `--line-mode simulation` or `--line-mode live`
on the existing launcher to enable one source. Put configuration and referenced
files in the private service data directory. The example is synthetic.

```json
{
  "line": {
    "simulation_fixture": "line-fixture.json",
    "tanks": {
      "Tank21": {
        "basket": "Tank21_HaveBasket",
        "door_opened": "Tank22_DoorOpened"
      },
      "Tank22": {}
    }
  }
}
```

A minimal `line-fixture.json` for that profile follows. Each row advances at
the worker cadence. Omit a signal or use null to simulate missing feedback.

```json
{
  "tags": [
    {"tag_name":"Tank21_HaveBasket","device_address":"M4","data_type":"Bool","read_write":"R","description":"Synthetic basket","multiplier":1},
    {"tag_name":"Tank22_DoorOpened","device_address":"M5","data_type":"Bool","read_write":"R","description":"Synthetic door","multiplier":1}
  ],
  "rows": [
    {"Tank21_HaveBasket":1,"Tank22_DoorOpened":0},
    {"Tank21_HaveBasket":1,"Tank22_DoorOpened":null}
  ]
}
```

For live reads, set `origin` to the confirmed base HTTP(S) origin, `clock_zone`
to the confirmed IANA timezone, and `clock_offset_s` to the measured seconds
to add to the supplier clock. Enter zero only if measured. Provide
`PLC_API_KEY` through the service environment. Restart for configuration
changes. Without confirmed time settings the browser can show API contact and
catalog metadata while PLC values remain Unknown. HTTP sends the credential
over the configured transport. Use HTTPS where the supplier supports it.

An optional `baseline` names a private JSON copy of an approved tag list in the
same shape as the list response. Catalog drift and duplicate addresses show as
warnings. The physical map uses explicit `TankNN` entries. Assign each
confirmed feedback tag exactly once. Use `basket` for the Bool shown on the
tank monitor and matching Scene anchor. For a door alias, enter the original
supplier tag only under the confirmed physical tank. Keep shared addresses
and disputed feedback out of the physical map. Scene's old `status_key` still
serves the Scene-only scripted fixture when line reads are disabled.

The worker preserves the supplier's engineering values, raw timestamp and row
ID. It separates HTTP 200 from PLC freshness. After two advancing rows,
values age out at 3 s. Missing or invalid values show as Unknown. The recorder
can sample any catalog tag with the same freshness rule and source identity.
Carry the local evidence files and exports separately from the repository.
