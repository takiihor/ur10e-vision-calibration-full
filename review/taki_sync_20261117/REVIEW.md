# Standalone module review — 2026-11-17

Review target: `takiihor/ur10e-vision-calibration-full`, base `0d0c5d4`.
Paths below are relative to `src/anodizing-modules/` unless stated otherwise.
This review covers software behavior, fixes and offline regressions. It does
not qualify a robot, camera, collision model or physical installation.

## Findings and fixes

| ID | Finding | Resolution |
| --- | --- | --- |
| F11 (high) | Bounded cleanup joins could return while SDK workers remained alive. `JobOwner._run` then released `owner.active`, allowing another job to acquire the same service. Manual RTDE cleanup, Teacher-close and teaching-abort were affected. | Add shared `JobOwner.drain_workers`: persist execution uncertainty and retain admission until live workers actually terminate. Drain outside the owner lock. A failed fence write records a storage error and retains the in-memory fence and ownership. Event-controlled regressions cover all three cleanup paths, competing admission and storage failures. |
| F1 | Numeric profile checks accepted NaN/Infinity; unordered bounds could pass and strict JSON serialization could fail. | Reject non-finite values, booleans and integers that overflow finite-float conversion. Test validation and capability serialization. |
| F2 | One-hour freshness applied to unversioned assets and a token-bearing iframe; main HTML was cacheable with revalidation. | Use `no-store` for token-bearing HTML and `no-cache` for other assets. Retain ETag, Last-Modified and CSP. In-memory HTTP tests cover revalidation and changed startup tokens. Previously cached responses require one cache-bypassing reload after upgrading. |
| F3 | The alphabetically first calibration supplied robot/TCP defaults for a new teaching project without explicit selection. | Keep available calibrations discoverable but leave calibration, robot and TCP as unselected placeholders unless a matching taught project or explicit profile supplies them. Filename order is not operator approval. |
| F4 | Removing site constants makes a reviewed profile or saved project necessary for deployment defaults; workspace bounds are required. | Preserve generic source defaults. Operators must supply their own reviewed scene, workspace, joint bounds and project/calibration selection through supported fields. No site profile or measured TCP is included. |
| F5 | The module launcher's default data directory is outside the source tree. | Retain the persistent-data default. Deployments that need a particular data directory should pass `--data-dir` explicitly. Repository-root `start-ui.sh` has a separate `.ui-data` default. |
| F6 | Invalid Basler exposure/gain silently became 5000 microseconds/0 dB. | Reject empty, non-finite or wrongly signed Basler entries before submission. Hidden Basler fields do not block RealSense. Keep server-side upper-range validation. Node fixtures exercise the shipped request builder. |
| F7 | Invalid Teacher overrides returned HTTP 200 with partial settings. | Return HTTP 400 with the field error. Missing, unreadable or malformed optional profile files retain the existing fallback; no strict whole-file schema is introduced. |
| F8 | Teaching prefill omitted workspace and descent joint bounds/source even though Manual/Robot fields were populated. | Restore the five Teaching fields from the same profile defaults. Preserve existing user edits. Node fixtures cover bounds/source and an edited workspace value. |
| F9 | Launcher executable checks rejected command names such as `ANODIZING_PYTHON=python`. | Use `command -v` in both module launchers. A temporary fake interpreter verifies command lookup without starting a service or devices. |
| F10 | The module workflow is nested below `src/anodizing-modules/.github/workflows/`, so it is not repository-root CI. | Retain the nested workflow. Repository-root CI remains outside this patch; no active CI is claimed. |

## F11 ownership and recovery

The job owner clears its active job after the job function returns or raises.
A timeout alone cannot establish that native SDK work has stopped. The shared
drain follows the existing bounded waits at all three cleanup boundaries and
prevents finalization while a listed worker remains alive. It retains a healthy
Teacher session's normal reuse behavior.

The regressions hold cleanup on explicit events and wait for the retained drain
or finalization before inspecting ownership under the owner lock. This avoids
an assertion that can pass just before premature finalization. Teacher abort
coverage includes the service, motion and nested SDK worker threads. Storage
failure must not bypass draining.

A permanently stuck worker keeps admission blocked until operator intervention.
The execution-uncertainty fence survives restart when it can be saved; storage
failure is reported and cannot establish durable recovery evidence. Physical
state, worker termination and saved outcomes require review before restart or
reconciliation. This is service-local exclusion, not controller-wide or
cross-host ownership, and it does not establish bounded physical stop timing.

## Verification and limits

The requested full runner was invoked once with CPython 3.12.16 and
`CHARUCO_OFFLINE=1`. It exited 1 after the pick/place suite; it is not a green
full-run result. The remaining two suites were then run once separately with
the same interpreter and runner-equivalent import paths/offline settings.

| Suite | Passed | Skipped | Failed | Duration |
| --- | ---: | ---: | ---: | ---: |
| App (full runner) | 351 | 8 | 0 | 133.64 s |
| Hand-eye (full runner) | 254 | 18 | 0 | 65.57 s |
| Pick/place (full runner) | 237 | 10 | 1 | 120.24 s |
| RealSense (separate run) | 112 | 8 | 0 | 10.04 s |
| Generator (separate run) | 203 | 0 | 0 | 35.55 s |

The failure was
`test_marker_validation.py::ValidationTests::test_tcp_change_or_rejected_stop_during_motion_is_reported`:
the saved status was `stopped`, while the test expected `failed`. Vendor source
and tests are byte-identical to the upstream base. One focused check against an
isolated upstream archive passed (1 test, 0.87 s); one focused candidate check
also passed (1 test, 0.88 s). These passes do not resolve the full-run failure
or establish its cause. No vendor fix, disabled test or full-run retry is
included. The failure remains a follow-up before claiming a green full suite.

The tests use fake hardware, temporary files, loopback HTTP and Node DOM
fixtures. Software regressions do not establish browser rendering, two-host
operation, physical motion, stop timing, camera accuracy or collision geometry.
Deployment profiles and physical confirmations remain operator responsibilities.
The portable nominal collision fixture is not evidence for a physical cell.

See [DELTA_REVIEW.md](DELTA_REVIEW.md) for the affected paths. Release packaging
must regenerate any source checksums for the resulting release; existing
provenance records describe their original source snapshots.
