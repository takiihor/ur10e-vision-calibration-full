#!/usr/bin/env bash
set -euo pipefail
bundle_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${ANODIZING_PYTHON:-$bundle_root/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  printf 'Set ANODIZING_PYTHON to the prepared Python interpreter.\n' >&2
  exit 2
fi
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
data_dir="${ANODIZING_DATA_DIR:-$state_home/anodizing-modules/data}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$bundle_root/app:$bundle_root/vendor/ur10e-handeye/src:$bundle_root/vendor/ur-pick-place/src:$bundle_root/vendor/charuco-realsense/src:$bundle_root/vendor/charuco-generator/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_bin" -B -m anodizing_web.server --data-dir "$data_dir" "$@"
