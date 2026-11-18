# Tool preview

The collision editor opens a tool box draft with a model and example scene for visual review.

## Sub-features

- `open-draft` checks the editor page exposed from Calibration.
- `preview-assets` checks the model and example scene use the same model digest and leave scene confirmation and coverage unset.
- `robot-motion` covers robot-card joint steps, moving to an observation pose, a checked joint test and vertical descent. Hardware-only, not driven by this skill. These paths require the real arm, selected factory model and reviewed scene and motion limits.

## How to get to it (user POV)

- In `Calibration` → `3 Plan` → `Advanced settings`, choose `Open full collision editor`.
- In `Teaching` → `Set up`, choose `Open tool box draft` for the same editor URL.
- The Calibration `2 Tool box` step embeds editor controls. Its JavaScript rendering is outside this HTTP recipe.

## Driving it with HTTP

Preconditions:

- Use the isolated baseline in [the map](README.md) with the bundled nominal model.

- **Open the collision editor and check its source assets.** Run `.agents/skills/verify-anodizing/scripts/verify tool-preview`. The Calibration link points to `/preview/tool-envelope.html`. The driver checks the editor title, then compares the model JSON hash with `/api/status` and `/preview/collision-scene.example.json`. Confirmation and cell coverage are false.
- **Proof.** Keep the request log, `tool-preview.html`, `ur10e-model.json`, `collision-scene.example.json` and `status.json`.

## Gotchas

- The example scene and nominal model do not qualify the robot or plan clearance. Rendering, editing, saving and hardware pose reads are outside this recipe.
