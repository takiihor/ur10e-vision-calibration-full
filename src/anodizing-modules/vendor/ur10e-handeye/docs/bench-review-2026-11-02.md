# Bench review: hand-eye, 2026-11-02

This branch contains reviewed capture/evidence and stop handling, fresh setup and replay inspection, and the bench camera-angle defaults. The automatic CLI defaults to 12.5° tilt and 15° roll for this bench, with a 30 mm probe; these values do not establish swept-path clearance or halve wrist movement. Other cells need their own reviewed settings.

Recorded camera verification used 14 fresh observations at replayed poses and passed, with maximum residuals 0.796243 mm and 0.182475°; it is not an independent pick-accuracy measurement. The calibration was ACCEPTABLE with an image-coverage warning. Earlier offline checks include 109 passed/17 skipped for affected tests, 24 stop checks, 9 reprocessing checks, and 2 final camera-angle/logging checks. An earlier full run had 174 passed, 17 skipped and 1 replay-serialization failure; it was fixed and followed by affected checks. Results are retained locally and are not new physical validation.

See [TODO](../TODO.md) for geometry, waypoint evidence, fixture portability and independent measurement work.
