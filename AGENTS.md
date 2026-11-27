# Working on the anodizing line

Start with the [operator and developer manual](docs/MANUAL.md) and
[architecture](docs/ARCHITECTURE.md). Use the [IPC module gateway](docs/MODULE_JOBS.md)
for client onboarding, saved operator enrollment and ownership. If an instruction or its safety
implications are ambiguous, ask the human before editing code or using hardware.

## Responsibilities

- Katana hosts the operator app and the built-in **Calibration module** in
  `programme.py`. The Calibration module owns test recipes, DRAFT limits and
  basket records. The planned **Main app loop** is for normal production.
- Route developer modules through the keyed HTTPS gateway on the IPC. Modules
  use scoped keys and APIs for all device, file and data operations. Use keyed
  APIs when adding cameras, UR10e, tower light or PLC data. Apply one owner at a time to the AGV page (`agv-page`), the Calibration
  module (`calibration`), named developer modules and the reserved
  `main-app-loop` owner. The built-in Calibration module needs no module
  token file. Reserve `main-app-loop` until its implementation is ready.
- The vehicle IPC owns the wired AGV link and executes one job at a time.
  Its job gateway owns credentials, ownership and admission. It keeps job
  evidence and software Stop state. Katana keeps the plan.
  The vehicle touchscreen is a read-only cached status display.
- Keep standalone Calibration, Teaching and Pick/place runnable without the
  operator app. Reuse their native cores and data. Keep operator app work
  within the requested integration.

| Work area | Source under `src/anodizing-modules/` |
| --- | --- |
| Calibration module, test recipes and limits | `app/anodizing_web/programme.py` |
| Katana-to-IPC client | `app/anodizing_web/executor_link.py` |
| IPC job gateway and key registry | `app/anodizing_web/job_gateway.py`, `app/anodizing_web/module_tokens.py` |
| IPC job execution and HTTPS service | `app/anodizing_web/executor.py`, `app/anodizing_web/executor_http.py` |
| AGV serial adapter | `app/anodizing_web/agv.py` |
| Katana HTTP API | `app/anodizing_web/server.py` |
| Operator UI | `operator-ui/` |

## Safety and verification

1. Route operator actions through the Katana app with an enrolled browser key.
   Keep the IPC HTTPS job listener keyed. The browser's Stop remains available
   before enrollment and Katana forwards it with its own service key. Only the authorized hardware adapter opens serial
   ports and sends device frames.
2. Use fake AGV, robot, executor and PLC devices by default. Live traffic
   requires an explicit request for that session and a human present.
   Only the operator at the physical AGV e-stop may arm or approve a live
   call. The supervisor's manual window permits individual calls.
   Live programme travel and dips remain disabled.
3. Preserve the priority Stop path. Latch Stop before ordinary work. Send
   the halt ahead of journal I/O. Fence any older command authority.
   Test Stop ordering when changing this path. Page Stop is software.
   The physical e-stop is the emergency control.
4. If a physical command has an uncertain outcome, read status, preserve
   the evidence and wait for Operator check. The executor holds and
   disarms. The operator decides whether to resume from a step or abort.
5. Keep proposed limits DRAFT until the process owner reviews and finalizes
   the revision. Measured breaches alarm, record and flag for inspection.
   The operator owns the disposition decision.

## Work and test

- Work on a dedicated branch from `vue`. Keep changes scoped and commits small.
  Preserve published history.
- For meaningful behavior changes and fixes, prove the failure with the
  existing offline test framework first, then make the smallest fix.
  Record what ran and distinguish offline evidence from any authorized
  physical checks.
- Use a fresh local data directory and a separate port for your app
  instance. Leave the Katana demo instance on port `8880` to its operators.
- Update documentation, verification evidence and remaining limits with
  each change. Use the [open findings](docs/OPEN_FINDINGS.md) for known issues.

From the repository root:

```bash
./start-ui.sh --help
(cd src/anodizing-modules && ./tools/test-all.bash)
(cd src/anodizing-modules/operator-ui && npm test)
(cd src/anodizing-modules/operator-ui && npm run build)
```

Set `ANODIZING_PYTHON` to a prepared interpreter. The
[manual](docs/MANUAL.md#developer-use) covers local simulation and the
[UI README](src/anodizing-modules/operator-ui/README.md#build-and-package)
covers dependencies and packaging.

## Public repository

Commit engineering settings, PLC tag and signal names, the name Katana,
port numbers and the documented private line-LAN addresses where needed.
Keep the real site profile, saved jobs, recordings, credentials, API keys,
passphrases, public or campus network identifiers, personal identifiers,
device serials and MAC addresses, Bluetooth identities, private links
and people's photos out of Git. Document site values with the
[example profile](docs/site-profile.example.json).
