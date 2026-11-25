# Vendored source provenance

This directory records where the bundled vendor trees came from. It is an offline
record because the relevant commits are not all reachable from the public
upstream main branches.

## Pinned baseline commits

| Vendor | Commit | Available from at packaging time |
| --- | --- | --- |
| `ur10e-handeye` | `2f0e69c776957772548506cc9532537792a03dca` | `fork/review/2026-11-03-calibration-collision` in the local clone |
| `ur-pick-place` | `e998dffc074c97bc3bd8f557564d0d1524de031f` | fetched object in the local clone; not contained in a listed branch |
| `charuco-generator` | `52093f99665e29d479b9a9e2c29b1aac05fee0d5` | local `main` in the clone used for packaging |
| `charuco-realsense` | `02e7010b7dcc63ed0dbfa5953ffe90a1d98b75fb` | local `master` in the clone used for packaging |

The archives were deterministic trees of those commits. They are proof of the
baseline, not a claim that the public upstream repositories contain these
commits. The `.tar.gz` archives have been removed from this repository; only
the recorded hashes and per-file deltas remain.

## The bundle is not the baseline

Every difference between the bundled vendor trees and these baselines is listed
in `BASELINES.json`. The changes include review patches, generated assets and
package-specific build files. `REVISIONS.txt` in the source bundle describes the
behavioral changes. Highlights:

- `ur10e-handeye`: collision/cartesian/preview work, Basler routing, generated
  preview build assets, and version `0.1.5+anodizing.1`.
- `ur-pick-place`: scheduling work, validation/teaching changes, and version
  `0.3.1+anodizing.1`.
- `charuco-realsense`: Basler adapter and package metadata, version
  `0.1.0+anodizing.1`.
- `charuco-generator`: package-relative config fallback and version
  `1.1.0+anodizing.1`.

The local development clones also contained uncommitted work when this bundle
was prepared (Basler follow-up work, pick/place manual/poses/UI edits, and an A3
calibration-board change). Those edits are **not** in this bundle and were not
merged here. Reconciling them requires the owner to identify the intended files.
See the limitation notes in the release documents.

## Verify offline

The baseline archives are not included in this repository. To run the verifier,
restore them under `provenance/` with the names recorded in `BASELINES.json`,
then run:

```bash
python3 provenance/verify.py
```

The script checks each archive hash, extracts it to a temporary directory, and
recomputes every added/changed/deleted entry in `BASELINES.json` against the
bundled vendor tree. For withheld private evidence, deletion entries
record the paths alone. The verified archive hash covers the original bytes.
The verifier reads files only.
