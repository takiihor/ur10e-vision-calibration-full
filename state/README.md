# IPC data snapshot

`ipc-data.tar.gz` is a snapshot of the IPC data directory:

- About 537 MB compressed and 647 MB uncompressed.
- 897 files with a `data/` prefix.
- Includes calibration sessions, teaching projects, scenes, board assets,
  job/evidence files and tool-power/MCU records.

No passwords, SSH keys, Wi-Fi profiles or browser tokens were found in the
snapshot. It is still confidential operational data: it can contain site layout,
part names, controller addresses and calibration results.

Handling rules:

1. Do not publish, email or attach this archive without owner approval.
2. Extract only into a dedicated review directory.
3. Compare `data-manifest.json` before and after inspection; do not edit the
   archive in place.
4. Do not treat the snapshot as a backup while the IPC is running. Stop writes or
   use the IPC's own backup procedure.
5. Delete the extracted copy when the review is complete, according to the
   owner's retention policy.

`data-manifest.json` lists the archived files and hashes. `PACKAGE_INFO.json`
records the archive hash and counts.
