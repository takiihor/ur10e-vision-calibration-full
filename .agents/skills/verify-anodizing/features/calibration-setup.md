# Calibration setup

The Calibration workspace opens on Setup with the bundled stage 2 calibration board selected.

## Sub-features

- `default-board` shows the board manifest and board name for a new setup.
- `calibration-run` covers previewing a live camera, capturing a board, pose feedback, planning, fitting and verification. Hardware-only, not driven by this skill. Camera and robot actions need site equipment and configured inputs. Saved-view and simulated calibration runs also stay out of the read-only workflow.

## How to get to it (user POV)

- Choose `Calibration` from the header or landing card. The first step is `1 Setup`.
- Find capture, planning, fit and verification controls in the Calibration steps.

## Driving it with HTTP

Preconditions:

- Use the isolated baseline in [the map](README.md). The launcher seeds the bundled board.

- **Open setup.** Run `.agents/skills/verify-anodizing/scripts/verify calibration-setup`. `GET /calibration` returns `Calibration` with `boards/calibration_stage2/board_manifest.json` in `#manifest`. The seeded manifest matches the source and contains `calibration_stage2`.
- **Proof.** Pair `calibration.html` and `requests.jsonl` with the fixture hash in `fixture.json`.

## Gotchas

- A missing or different manifest fails this journey. A bundled board does not establish camera framing, pose feedback or calibration accuracy.
