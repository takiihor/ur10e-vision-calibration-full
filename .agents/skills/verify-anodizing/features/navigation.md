# Module navigation

The landing page links to the Calibration, Teaching, Pick/place and AGV workspaces.

## Sub-features

- `open-calibration` opens the Calibration workspace.
- `open-teaching` opens Marker Pick Teacher.
- `open-pick-place` opens the Pick/place test workspace.

## How to get to it (user POV)

- Open `/` and choose a module in the header or from its card.
- The same header links appear on the three module pages.

## Driving it with HTTP

Preconditions:

- Use the isolated baseline in [the map](README.md).

- **Open each module.** Run `.agents/skills/verify-anodizing/scripts/verify navigation`. The driver finds the landing links and GETs `/calibration`, `/teaching`, `/pick-place` and `/agv`. Each returns 200 with its own title.
- **Proof.** Compare `requests.jsonl`, `index.html`, `calibration.html`, `teaching.html`, `pick-place.html` and `agv.html`.

## Gotchas

- Browser layout and client-side navigation are outside this HTTP check. A successful landing page alone does not verify the linked modules.
