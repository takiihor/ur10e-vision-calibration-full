# Anodizing verification map

Use this index to find the user path and the evidence that shows it works.

## Baseline preconditions

- Follow [Launch and Doctor](../SKILL.md) in the checkout you intend to verify. Each command creates a new isolated instance and runs Doctor first.
- Start with an empty `/tmp/ui-data` in the private namespace. The launcher copies the bundled boards. No saved IPC data, calibration, project or site profile is required.
- The app listens on `127.0.0.1:8871` inside the isolated namespace. Use only the helper's HTTP driver.

## Driving conventions

- Run commands from the checkout root. Use the literal feature IDs below.
- Follow only GET routes named in the recipes. Stop before sending a request that can start work or open a device.
- Treat a missing fixture or failed request as a failed journey. Keep its evidence.

## Proof and skip reporting

- Pair each request in `requests.jsonl` with its response artifact and its expected state. Preserve fixture digests and any observed effect on scratch storage.
- Keep `revision.txt`, `network.strace`, `isolation.json` and `cleanup.json` with the journey artifacts.
- An HTML response verifies the server surface only. Report any claim that requires client-side behavior or hardware as unverified.

## Features

- [Module navigation](navigation.md) opens each module from the landing page.
- [Calibration setup](calibration-setup.md) checks the bundled default board and documents calibration-run exclusions.
- [Teaching setup](teaching-setup.md) checks prefilled inputs and board discovery and documents teaching exclusions.
- [Pick/place availability](pick-place-availability.md) checks disabled hardware capabilities and documents job-execution exclusions.
- [Tool preview](tool-preview.md) checks the model and example collision scene and documents motion exclusions.
