# Three test modules and IPC controller

This package contains the current standalone Calibration, Teaching and Pick/place
pages, their shared IPC service and native module cores.
The three modules run without the ano app or ROS.

## Package contents

- `src/anodizing-modules/`: the reviewed source, test, packaging and board
  files in this revision.
- `state/README.md`: handling notes for private IPC data.
- `environment/requirements.txt`: Python dependency pins.
- `review/`: historical review index and synthetic calibration example.
- `provenance/`: public vendor file mapping and offline verifier. Supply the
  trusted baseline record and archives from private storage.
- `RELEASE_NOTES.md`: changes, limitations and unreconciled work.
- `PACKAGE_INFO.json`: package description and external requirements.
- `CONTENTS.sha256`: hashes of every other file in the package.

SSH keys, passwords, Wi-Fi profiles, browser tokens, virtual environments,
SDK wheels and Python caches are outside this package.

## Split between the PCs

The IPC runs `app/anodizing_web/server.py`. Its shared job owner admits work from
all three pages. The native `ur_rtde` backend performs robot operations; the
shared camera code supplies RealSense or the prepared Basler path. The service
owns planning, execution, saved data and MCU requests.

The laptop runs the browser and an SSH tunnel. It sends operator requests and
approvals, then displays IPC status. It does not need a local camera SDK or a
second robot controller. This package does not add a separate controller daemon.

## Verify after extraction

From this package's top directory:

```bash
sha256sum -c CONTENTS.sha256
```

`SOURCE_MANIFEST.sha256` and the source-level `SHA256SUMS` are historical
inventories with withheld entries omitted.

Verify the offline vendor provenance separately:

```bash
python3 provenance/verify.py /private/baselines/BASELINES.json
```

Run the full software suite with one command:

```bash
cd src/anodizing-modules
ANODIZING_PYTHON=/path/to/python ./tools/test-all.bash
```

## Start the UI quickly

From this package's top directory:

```bash
./start-ui.sh
```

The first run creates `.venv`, installs the base Python dependencies and copies
the bundled boards into `.ui-data`. Open `http://127.0.0.1:8870/`. Hardware
access stays off unless opt-in flags are passed; run `./start-ui.sh --help`.

Stop it with:

```bash
./kill.sh
```

Use `./kill.sh PORT` for another port, or `./kill.sh --all` for all instances.

## First software check

Use a separate virtual environment. On Linux with Python 3.12:

```bash
python3.12 -m venv review-venv
review-venv/bin/python -m pip install -r environment/requirements.txt
```

The third-party requirements and the vendored project metadata describe
the dependencies. SDK binaries and operating-system packages still need installation
on a new PC. The launcher supplies all four bundled native modules through
`PYTHONPATH`; a fresh checkout from GitHub is not required.

Use a new data directory for the first check. Keep the saved snapshot separate.
From the package's top directory, with an existing prepared interpreter:

```bash
export MODULE_BUNDLE="$PWD/src/anodizing-modules"
export ANODIZING_PYTHON="$PWD/review-venv/bin/python"
export ANODIZING_DATA_DIR="$PWD/offline-check-data"
mkdir -p "$ANODIZING_DATA_DIR"
cp -an "$MODULE_BUNDLE/data/boards" "$ANODIZING_DATA_DIR/"
bash "$MODULE_BUNDLE/tools/probe.bash"
bash "$MODULE_BUNDLE/tools/run-ipc.bash" --port 8871
```

This starts the service with hardware opt-ins off on a separate review port.
The service has no built-in
robot address, scene, camera serial, joint bounds or named teaching project. It
discovers board/project/calibration files under `ANODIZING_DATA_DIR`; reviewed
overrides can be placed in an optional `site-profile.json` there. In another
terminal with the same variables, run:

```bash
ANODIZING_PORT=8871 "$ANODIZING_PYTHON" -B "$MODULE_BUNDLE/tools/smoke.py"
```

For a remote browser, forward this offline port from the laptop:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8871:127.0.0.1:8871 USER@IPC_HOST
```

Open `http://127.0.0.1:8871/calibration`, `/teaching` or `/pick-place`.
The source README contains the detailed native and hardware configuration.

## Saved data and physical testing

The data snapshot is not included in this public source repository. It is an
archive, not an automatic restore or resume instruction. Its `.jobs` records
retain interrupted-job and execution-uncertainty evidence. Review those records
before any restoration. Preserve recovery evidence; copying the source does not
establish the physical robot or item state.

If you have the separate local package, extract the archive only in a dedicated
review directory, as described in `state/README.md`. Do not add the extracted
data to this repository.

Keep the installation's launch configuration in local storage. Hardware use
requires the correct camera serial, active TCP, robot model, scene, calibration
and explicit task approval.

Current physical limits:

- Joint moves can curve and change TCP height. Their workspace bounds apply to
  the start and endpoint. The selected scene checks the joint path.
- The supervised-self scene omits external cell objects.
- MCU acknowledgements do not prove that the rod is held or released.
- Verify camera intrinsics with independent views before metric use. Keep
  measurements, camera identity and optical settings in the private calibration
  record. See the [encoder-plate workflow](tools/ENCODER_INTRINSICS.md).

Packaging reads files only. It does not provide new physical validation.
