# UR10e vision calibration

Calibration, teaching and pick/place tooling for a UR10e with Intel RealSense
and Basler cameras. The package combines:

- Eye-in-hand and eye-to-hand calibration using ChArUco boards.
- Intel RealSense and Basler camera adapters.
- An IPC web service for Calibration, Teaching and Pick/place.
- Checked joint-path and collision-scene integration.
- Offline tests, vendor provenance and recorded baseline deltas.

The source bundle is in [`src/anodizing-modules/`](src/anodizing-modules/).
Start with [`START_HERE.md`](START_HERE.md), then the module
[`README.md`](src/anodizing-modules/README.md).

## Start the UI

```bash
./start-ui.sh
```

The first run creates `.venv`, installs the Python dependencies and copies the
bundled boards into `.ui-data`. It then starts the local service and opens
`http://127.0.0.1:8870/`. Use `./start-ui.sh --help` for ports, data directories
and hardware opt-in flags. Hardware access is off by default.

## Verify

```bash
sha256sum -c CONTENTS.sha256

cd src/anodizing-modules
sha256sum -c ../../SOURCE_MANIFEST.sha256
cd ../..

python3 provenance/verify.py
```

## Test

```bash
cd src/anodizing-modules
ANODIZING_PYTHON=/path/to/python ./tools/test-all.bash
```

## Data snapshot

The local packaging bundle includes a 513 MB IPC data snapshot. It is omitted
from this public source repository because it exceeds GitHub's per-file limit
and contains confidential site calibration and job data. See
[`state/README.md`](state/README.md).

## Status and limits

No physical robot, camera, MCU or IPC service was validated while preparing this
revision. Offline tests do not establish clearance, timing, accuracy or safety.
See [`RELEASE_NOTES.md`](RELEASE_NOTES.md) for the unresolved vendor WIP and
licensing limitations.

## License

No license grant is included. Copyright remains with the respective authors.
See [`LICENSE-NOTICE.md`](src/anodizing-modules/LICENSE-NOTICE.md) and the vendor
notices before redistributing.
