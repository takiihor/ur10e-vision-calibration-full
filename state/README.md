# IPC data snapshot

The private IPC archive can contain calibration sessions, teaching projects,
scenes, board assets, job evidence and tool-power/MCU records.

Treat the archive as confidential operational data. It can identify the site
layout, parts, controller addresses and calibration results.

Handling rules:

1. Do not publish, email or attach this archive without owner approval.
2. Extract only into a dedicated review directory.
3. Compare `data-manifest.json` before and after inspection; do not edit the
   archive in place.
4. Do not treat the snapshot as a backup while the IPC is running. Stop writes or
   use the IPC's own backup procedure.
5. Delete the extracted copy when the review is complete, according to the
   owner's retention policy.

Keep the file manifest and archive checksum with the private archive. Use
them to verify an owner-supplied copy before extraction.
