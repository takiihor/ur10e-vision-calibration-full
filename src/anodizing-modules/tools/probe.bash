#!/usr/bin/env bash
set -euo pipefail
bundle_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${ANODIZING_PYTHON:-$bundle_root/.venv/bin/python}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$bundle_root/app:$bundle_root/vendor/ur10e-handeye/src:$bundle_root/vendor/ur-pick-place/src:$bundle_root/vendor/charuco-realsense/src:$bundle_root/vendor/charuco-generator/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_bin" -B "$bundle_root/tools/probe.py"
