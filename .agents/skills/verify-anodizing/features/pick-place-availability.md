# Pick/place availability

The Pick/place page reports whether the service offers manual movement, tool voltage and MCU commands.

## Sub-features

- `manual-capability` checks that manual Pick/place is unavailable on the offline instance.
- `peripheral-capability` checks the cached tool power, MCU and robot control capability replies.
- `execute-job` covers manual and repeated PICK / PLACE, approvals, stop and recovery, tool power and MCU commands. Hardware-only, not driven by this skill. They can change the arm, tool or held item. Simulated PICK / PLACE submissions stay out of the read-only workflow.

## How to get to it (user POV)

- Choose `Pick/place` in the header or `Pick/place test` on the landing card.
- The `Advanced — single PICK / PLACE`, `Automatic rod transfer test` and peripheral controls are on the same page.
- Teaching can hand off to this page after an actual teaching flow. That entry needs a completed teaching project and stays outside this skill.

## Driving it with HTTP

Preconditions:

- Use the isolated baseline in [the map](README.md) with all hardware flags off.

- **Open the page and check availability.** Run `.agents/skills/verify-anodizing/scripts/verify pick-place-availability`. The driver GETs `/pick-place` and checks `/api/manual-pick-place`, `/api/tool-power`, `/api/mcu` and `/api/robot-control`. Manual hardware availability is false and the three other controls report unavailable.
- **Proof.** Keep `pick-place.html`, `requests.jsonl` and the four capability JSON files.

## Gotchas

- Capability GETs report configuration and cached status. Live commands and physical state require their own hardware procedure.
