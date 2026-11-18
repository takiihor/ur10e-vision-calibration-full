# Teaching setup

Marker Pick Teacher discovers printed picking boards and preselects a target when opened on fresh data.

## Sub-features

- `setup-defaults` loads setup fields and printed picking board choices.
- `teach-pick` covers connecting the setup, capturing and teaching a pick, reading the marker, exporting poses, and validation. Hardware-only, not driven by this skill. These steps require reviewed camera, robot and calibration inputs. Simulation job submissions stay out of the read-only workflow.

## How to get to it (user POV)

- Choose `Teaching` from the header or landing card. `Set up` shows `Printed picking board`.
- The subsequent Teaching steps lead to capture, teaching, export and validation. Stop before `Check setup & continue`.

## Driving it with HTTP

Preconditions:

- Use the isolated baseline in [the map](README.md), including the seeded 80 mm board manifest and empty project and calibration folders.

- **Open Set up.** Run `.agents/skills/verify-anodizing/scripts/verify teaching-setup`. The driver follows the Teaching link, checks `#picking-board` and GETs the authenticated `/api/teacher-defaults` request used by `teacher.js`. The default target is a bundled 80 × 80 mm picking board and appears in the choices. Fresh state returns no saved calibration or robot address.
- **Proof.** Pair `requests.jsonl` and `teaching.html` with `teacher-defaults.json` and `fixture.json`. The manifest matches the source and setup creates no project or job.

## Gotchas

- A missing 80 mm manifest or default is a failure. The expected empty calibration list is a consequence of fresh state.
- The defaults response is the data used to populate the dropdown. JavaScript rendering and opening a real saved project are outside this journey.
