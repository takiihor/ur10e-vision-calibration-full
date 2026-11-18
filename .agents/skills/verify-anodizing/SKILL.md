---
name: verify-anodizing
description: Verify the anodizing Interface PC web app headlessly through its user-facing HTTP pages and read-only API. Use for changes to Calibration setup, Teaching setup, Pick/place availability, module navigation, or the tool preview.
---

# Verify the anodizing interface

## Launch

From the checkout root on Linux with `bwrap` (Bubblewrap), `strace`, `ss`, Git and a prepared CPython 3.12 interpreter with the app dependencies, run:

```bash
export ANODIZING_PYTHON=/absolute/path/to/venv/bin/python
.agents/skills/verify-anodizing/scripts/verify teaching-setup
```

With `ANODIZING_PYTHON` unset, the helper uses the launcher's existing `${ANODIZING_VENV:-.venv}/bin/python` from the checkout root. Prepare that environment in advance or set `ANODIZING_PYTHON` to your interpreter. The helper creates no environment and installs no dependencies.

The helper runs the repository's `./start-ui.sh --no-browser --port 8871 --data-dir /tmp/ui-data` inside a fresh network and PID namespace. The app uses its normal launcher to copy bundled boards and initialize job storage. A private `/tmp` gives the app an independent data directory and service lock. The checkout and interpreter are read-only mounts. The isolated process can only reach its own loopback and has a private `/dev`; it has no robot route or camera devices. Keep all hardware flags off and use only the new data directory. Never fall back to direct launch if isolation is unavailable.

The helper waits for the listener and runs Doctor before any feature request. The `Doctor` line means the authenticated app is ready with hardware disabled. Any failed phase exits nonzero. `server.log` records launcher and server errors.

## Doctor

```bash
.agents/skills/verify-anodizing/scripts/verify doctor
```

This runs just the read-only checks against a fresh verification instance, then shuts it down. It checks that the launched process owns `127.0.0.1:8871`, the landing token works, `/api/status` reports the app ID and scratch path, the model is valid, the job owner is idle, and hardware opt-ins and manual movement are disabled. `revision.txt` records the checkout's Git HEAD and dirty files. The helper never targets a server in the host namespace.

## Drive

Use the feature-specific commands and expected results in [the feature map](features/README.md). Each command uses the same pages and GET routes as the user, then checks the response and where applicable the files backing it. Keep requests read-only. Stop before any path requiring a robot connection, camera, teaching action, calibration run, or job execution. HTTP proof covers responses and local data. It leaves browser rendering, client-side behavior and field hardware untested.

## Evidence

The helper prints a unique `anodizing-verify.*` directory under `/tmp`. To retain it in an existing durable directory, pass that directory as the second argument:

```bash
.agents/skills/verify-anodizing/scripts/verify teaching-setup /absolute/path/to/evidence
```

Read `requests.jsonl` alongside the response HTML and JSON for the user action and its outcome. Saved HTML redacts the module token. Fixture evidence records its SHA-256 and state checks where applicable. `isolation.json` records the namespace and interfaces before startup. `network.strace` records the launched app's socket calls. A passing run shows its loopback bind and accepted HTTP traffic, zero IP `connect` calls, and no socket address for `192.168.12.248` or the UR ports `29999`, `30001`–`30004`, `30020`. Use `cleanup.json` and `result.json` to confirm shutdown and success. Retain logs and report the unmet precondition on failure.

## Cleanup

The helper sends SIGINT to its own child and waits for it. It verifies that the listener is gone. Bubblewrap disposes of the private process namespace and temporary data on exit, including on failure. The host evidence directory survives. Do not use `kill.sh` for verification.

## Helpers

Use `scripts/verify FEATURE [EVIDENCE_PARENT]` as shown above. It invokes `python -I -B /checkout/.agents/skills/verify-anodizing/scripts/verify.py FEATURE HOST_NETWORK_NAMESPACE` inside Bubblewrap. The Python driver checks the namespace before launching anything.
