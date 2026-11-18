#!/usr/bin/env bash
# Run every bundled Python suite with one command. Browser checks skip if Node/Chrome are absent.
set -euo pipefail

bundle_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${ANODIZING_PYTHON:-$bundle_root/.venv/bin/python}"
if ! command -v -- "$python_bin" >/dev/null; then
  printf 'Set ANODIZING_PYTHON to a Python 3.11+ interpreter with pytest and the app requirements.\n' >&2
  exit 2
fi
export PYTHONDONTWRITEBYTECODE=1
export CHARUCO_PYTHON="$python_bin"
common="$bundle_root/app:$bundle_root/vendor/ur-pick-place/src:$bundle_root/vendor/ur-pick-place/tests:$bundle_root/vendor/ur10e-handeye/src:$bundle_root/vendor/charuco-realsense/src:$bundle_root/vendor/charuco-generator/src"

run() {
  local label="$1"; shift
  printf '\n== %s ==\n' "$label"
  "$python_bin" -m pytest -q -p no:cacheprovider "$@"
}

PYTHONPATH="$common" run 'app' "$bundle_root/app/test"
PYTHONPATH="$bundle_root/vendor/ur10e-handeye/src:$bundle_root/vendor/charuco-realsense/src:$bundle_root/vendor/charuco-generator/src" \
  run 'ur10e-handeye' "$bundle_root/vendor/ur10e-handeye/tests"
PYTHONPATH="$bundle_root/vendor/ur-pick-place/src:$bundle_root/vendor/ur-pick-place/tests" \
  run 'ur-pick-place' "$bundle_root/vendor/ur-pick-place/tests"
PYTHONPATH="$bundle_root/vendor/charuco-realsense/src:$bundle_root/vendor/charuco-generator/src" \
  run 'charuco-realsense' "$bundle_root/vendor/charuco-realsense/tests"
PYTHONPATH="$bundle_root/vendor/charuco-generator/src" \
  run 'charuco-generator' "$bundle_root/vendor/charuco-generator/tests"

printf '\nAll selected suites passed (browser checks may have skipped).\n'
