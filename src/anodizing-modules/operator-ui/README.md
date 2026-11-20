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

The Sakai follow-up was verified with fake ports, software tests and the in-process simulator. Field serial, radio timing, physical e-stop and site acceptance require hardware verification.
