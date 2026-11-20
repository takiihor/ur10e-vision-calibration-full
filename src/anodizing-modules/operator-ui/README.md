# Operator app stage 1

Open `/operator/` on the local Anodizing Modules service. Home reports service and AGV state. The AGV link opens commissioning. Calibration, Teaching and Pick/place open their existing standalone pages. The original landing page and AGV link are the defaults.

## Build and package

From this directory on a development machine with Node 22 and npm:

```bash
npm ci --offline --cache /path/to/prepared/npm-cache --ignore-scripts --no-audit --no-fund
npm test -- --maxWorkers=2
npm run build
```

The exact dependency versions and integrity hashes are in `package-lock.json`. The Sakai 5.0.0 layout is trimmed to navigation and a content region for the two routes. PrimeVue uses Aura, the app’s own control and status colours, local PrimeIcons and system fonts. The notices accompany the release artifacts.

Vite writes `../app/anodizing_web/operator_dist/`, including the hashed assets and HTTP allowlist. Commit the generated output with the UI changes. Build the Python wheel from a clean app source directory with the generated output present. The Python package includes the entry, asset allowlist, fonts, CSS, JavaScript and notices. The IPC only needs the Python package and its usual dependencies. For local iteration, use `npm run build -- --watch` with the normal Python service in simulation.

## State and controls

The app sends the injected `X-Module-Token` header on same-origin API requests. The entry is served fresh with a per-response PrimeVue style nonce. A service restart requires a reload and a deliberate new connection. AGV status reads use `/api/state` and `/api/agv?cached=1`. `/api/agv?ports=1` refreshes the port list and command capabilities. AGV operations use the existing `POST /api/agv`.

The commissioning view uses the same connection, checklist, notes and record storage keys as the legacy AGV page. Open the AGV route to use the simulator, then choose **Simulator** and **Connect**. The toolbar identifies the AGV Stop and Pause targets. Escape requests the AGV software Stop. Robot stops for active Manual and Teacher sessions display their own buttons and use their own bound job IDs. Opening the AGV route on an armed link requests disarm. Inspect the link before issuing any new request after a failure.

Stage 1 evidence is from fake ports, software tests and the in-process simulator. Field serial, radio response, physical e-stop, hardware and site acceptance remain separate.
