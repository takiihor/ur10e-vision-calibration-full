# Vendored source provenance

The release owner keeps a trusted copy of the original `BASELINES.json` with
the four matching source archives in private storage. That record contains
the baseline revisions, archive names and their expected SHA-256 checksums.

## Published vendor sources

The public `BASELINES.json` lists each shipped file as unchanged, changed
or added relative to the trusted baseline. Changes include review patches,
generated assets and package-specific build files. `REVISIONS.txt` in the source bundle describes the
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

Obtain the trusted baseline record from the release owner. Keep the matching
archives next to that record and pass its path to the verifier.

```bash
python3 provenance/verify.py /private/baselines/BASELINES.json
```

With no argument the verifier reads `provenance/private/BASELINES.json`.
Git ignores that directory. Verify the record's authenticity independently
before using it to check an archive.

The verifier checks every archive against the trusted checksum before
extraction. It compares every shipped file with its recorded category and
checks the published digest for each addition and change. The complete list
of unchanged, changed and added files also detects a restored or missing file.
