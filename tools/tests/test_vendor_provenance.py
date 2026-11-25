"""Verify a vendor tree without disclosing its private baseline metadata."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


class VendorProvenanceTests(unittest.TestCase):
    def test_private_archive_authenticates_omitted_files_and_published_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = root / "provenance"
            public.mkdir()
            verifier = public / "verify.py"
            shutil.copyfile(Path(__file__).resolve().parents[2] / "provenance/verify.py", verifier)
            vendor = root / "src/anodizing-modules/vendor/demo"
            vendor.mkdir(parents=True)
            (vendor / "source").write_text("public example")
            private = root / "private"
            private.mkdir()
            omitted = private / "sample-note"
            omitted.write_text("synthetic")
            archive = private / "example.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(omitted, arcname="demo/sample-note")
                tar.add(vendor / "source", arcname="demo/source")
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            trusted = private / "BASELINES.json"
            trusted.write_text(json.dumps({"baselines": {"demo": {
                "archive": archive.name, "sha256": checksum,
            }}}))
            (public / "BASELINES.json").write_text(json.dumps({"vendors": {"demo": {
                "unchanged": ["source"], "added": [], "changed": [],
            }}}))

            def verify():
                return subprocess.run([sys.executable, verifier, trusted], capture_output=True, text=True)

            result = verify()
            self.assertEqual(result.returncode, 0, result.stderr)
            shutil.copyfile(omitted, vendor / omitted.name)
            self.assertIn("bundle deltas differ", verify().stderr)
            archive.write_bytes(b"modified")
            self.assertIn("sha256", verify().stderr)
