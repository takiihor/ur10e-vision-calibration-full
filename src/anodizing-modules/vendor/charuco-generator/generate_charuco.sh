#!/usr/bin/env bash
# Set up this checkout and run the ChArUco CLI. No activation or global pip needed.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$PROJECT_DIR"
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
VENV_DIR="${CHARUCO_VENV:-$state_home/charuco-generator/venv}"
if [[ -n "${CHARUCO_PYTHON:-}" ]]; then
    VENV_PYTHON="$CHARUCO_PYTHON"
else
    VENV_PYTHON="$VENV_DIR/bin/python"
fi

python_is_supported() {
    "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1
}

if [[ -n "${CHARUCO_PYTHON:-}" && ! -x "$VENV_PYTHON" ]]; then
    printf 'Error: CHARUCO_PYTHON must point to a working Python executable.\n' >&2
    exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
    SELECTED_PYTHON=""
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        if command -v "$PYTHON_BIN" >/dev/null 2>&1 && python_is_supported "$PYTHON_BIN"; then
            SELECTED_PYTHON="$PYTHON_BIN"
        else
            printf 'Error: PYTHON_BIN must point to a working Python 3.11+ interpreter.\n' >&2
            exit 1
        fi
    else
        # Prefer the tested Python 3.12 when available, then other supported versions.
        for candidate in python3.12 python3.11 python3.13 python3.14 python3; do
            if command -v "$candidate" >/dev/null 2>&1 && python_is_supported "$candidate"; then
                SELECTED_PYTHON="$candidate"
                break
            fi
        done
    fi
    if [[ -z "$SELECTED_PYTHON" ]]; then
        printf 'Error: install Python 3.11+ with venv support, then run this script again.\n' >&2
        printf 'You can choose an interpreter with PYTHON_BIN=/path/to/python ./generate_charuco.sh\n' >&2
        exit 1
    fi
    printf 'Creating isolated environment with %s in %s...\n' "$SELECTED_PYTHON" "$VENV_DIR"
    if ! "$SELECTED_PYTHON" -m venv "$VENV_DIR"; then
        printf 'Error: could not create the environment. Install venv support for your Python (python3-venv on Debian/Ubuntu).\n' >&2
        exit 1
    fi
fi

if ! python_is_supported "$VENV_PYTHON"; then
    printf 'Error: the existing environment needs Python 3.11+. Move %s aside and rerun with PYTHON_BIN=/path/to/python.\n' "$VENV_DIR" >&2
    exit 1
fi

if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
    "$VENV_PYTHON" -m ensurepip --upgrade
fi

# Avoid invoking pip or contacting the network when every pinned package is installed.
if ! "$VENV_PYTHON" - "$PROJECT_DIR/requirements.txt" <<'PY'
import importlib.metadata
from pathlib import Path
import sys

try:
    from packaging.requirements import Requirement
    for line in Path(sys.argv[1]).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        if not requirement.specifier.contains(importlib.metadata.version(requirement.name)):
            raise ValueError(requirement.name)
except (ImportError, importlib.metadata.PackageNotFoundError, ValueError):
    raise SystemExit(1)
PY
then
    if [[ "${CHARUCO_OFFLINE:-0}" == 1 ]]; then
        printf 'Error: required packages are missing and CHARUCO_OFFLINE=1 forbids network installation.\n' >&2
        printf 'Install them from a reviewed local wheel set or unset CHARUCO_OFFLINE.\n' >&2
        exit 1
    fi
    printf 'Installing pinned dependencies...\n'
    export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$VENV_DIR/pip-cache}"
    if ! "$VENV_PYTHON" -m pip install --disable-pip-version-check --quiet -r "$PROJECT_DIR/requirements.txt"; then
        printf 'Error: dependency installation failed. Check the pip error above and your package source, then rerun.\n' >&2
        exit 1
    fi
    "$VENV_PYTHON" -m pip check
fi

if [[ $# -eq 0 ]]; then
    if [[ -t 0 ]]; then
        set -- menu
    else
        set -- generate --config config/boards.json
    fi
elif [[ "$1" == -* && "$1" != "--help" && "$1" != "-h" ]]; then
    # Convenience: ./generate_charuco.sh --output-dir output/custom
    set -- generate "$@"
fi

exec "$VENV_PYTHON" "$PROJECT_DIR/generate.py" "$@"
