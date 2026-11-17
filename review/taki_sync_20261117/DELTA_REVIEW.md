# Patch scope and preservation

Base: `0d0c5d4` in `takiihor/ur10e-vision-calibration-full`.
Paths below are relative to `src/anodizing-modules/`.
The patch preserves the standalone module layout, native hardware adapters and
generic site-profile model. No deployment profile or site constants are added.

| Paths | Change and evidence |
| --- | --- |
| `app/anodizing_web/jobs.py` | Shared uncertainty fencing and worker drain retain job ownership through SDK termination (F11). |
| `app/anodizing_web/manual_execution.py` | Call the shared drain after manual cleanup and Teacher-close bounded waits; a failed fence save cannot bypass cleanup (F11). |
| `app/anodizing_web/teaching.py` | Require explicit calibration selection (F3); retain Teacher ownership through abort cleanup (F11). |
| `app/anodizing_web/site_profile.py` | Reject non-finite, boolean and overflowing numeric bounds (F1). |
| `app/anodizing_web/server.py` | Correct cache policy and reject invalid Teacher overrides (F2, F7). |
| `app/anodizing_web/web/app.js` | Validate Basler exposure/gain before submission (F6). |
| `app/anodizing_web/web/test-profile.js` | Restore Teaching workspace and descent joint bounds/source without replacing user edits (F8). |
| `tools/run-ipc.bash`, `tools/test-all.bash` | Accept executable command names through shell lookup (F9). |
| `app/test/test_job_cleanup.py`, `app/test/test_manual_execution.py` | Event-controlled live-worker, admission, uncertainty-persistence and storage-failure regressions (F11). |
| `app/test/test_site_profile.py` | Numeric validation, JSON safety and explicit calibration selection (F1, F3). |
| `app/test/test_handler_defaults.py`, `app/test/test_port.py` | In-memory HTTP errors, startup tokens, cache headers and revalidation (F2, F7). |
| `app/test/test_web_profile.py` | Run the shipped request builder and prefill code with offline Node DOM fixtures (F6, F8). |
| `app/test/test_launchers.py` | Exercise both launchers with a fake interpreter found through PATH (F9). |
| `README.md` | Describe validated inputs, cache behavior and ownership/recovery limits. |

F4/F5 document profile and persistent-data migration requirements without
restoring site values. F10 records the existing nested workflow limitation;
repository-root CI is outside this patch. Vendor source and repository-root
launchers/intrinsics tools are unchanged. The patch does not requalify bundled
nominal fixtures, install dependencies or validate physical hardware.
