#!/usr/bin/env bash
# Start the three anodizing web UIs (Calibration, Teaching, Pick/place).
# First run creates .venv, installs Python dependencies and copies the bundled
# calibration boards into .ui-data. Hardware access stays disabled unless you
# pass the server's own opt-in flags (for example --allow-camera).
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$ROOT/src/anodizing-modules"
VENV="${ANODIZING_VENV:-$ROOT/.venv}"
PORT="${ANODIZING_PORT:-8870}"
DATA_DIR="${ANODIZING_DATA_DIR:-$ROOT/.ui-data}"
OPEN_BROWSER="${ANODIZING_OPEN_BROWSER:-1}"
INSTALL_HARDWARE=0
FORCE_INSTALL=0
SERVER_ARGS=()

usage() {
  cat <<EOF
Usage: ./start-ui.sh [options] [server options]

Starts the local IPC service and opens the landing page:
  http://127.0.0.1:${PORT}/

Options:
  --port PORT          Listen port (default: 8870, env ANODIZING_PORT)
  --data-dir DIR       Data directory (default: .ui-data, env ANODIZING_DATA_DIR)
  --no-browser         Do not open a browser
  --install            Reinstall Python dependencies
  --install-hardware   Also install camera/robot libraries
  -h, --help           Show this help

Any other options are passed to the IPC server. Common examples:
  --allow-camera
  --allow-teaching
  --allow-manual
  --allow-manual-pick-place
  --allow-agv-in-recipe

Environment:
  ANODIZING_PYTHON     Use an existing prepared interpreter; skips .venv setup
  ANODIZING_OFFLINE=1  Never run pip; fail with the install command instead
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --port)
      (($# >= 2)) || die "--port needs a value"
      PORT="$2"
      shift 2
      ;;
    --port=*)
      PORT="${1#*=}"
      shift
      ;;
    --data-dir)
      (($# >= 2)) || die "--data-dir needs a value"
      DATA_DIR="$2"
      shift 2
      ;;
    --data-dir=*)
      DATA_DIR="${1#*=}"
      shift
      ;;
    --no-browser)
      OPEN_BROWSER=0
      shift
      ;;
    --install)
      FORCE_INSTALL=1
      shift
      ;;
    --install-hardware)
      FORCE_INSTALL=1
      INSTALL_HARDWARE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      SERVER_ARGS+=("$@")
      break
      ;;
    *)
      SERVER_ARGS+=("$1")
      shift
      ;;
  esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] || die "invalid port: $PORT"
((PORT >= 1 && PORT <= 65535)) || die "port must be 1 to 65535"

if [[ -n "${ANODIZING_PYTHON:-}" ]]; then
  PYTHON_BIN="$ANODIZING_PYTHON"
  [[ -x "$PYTHON_BIN" ]] || die "ANODIZING_PYTHON is not executable: $PYTHON_BIN"
else
  BASE_PYTHON=''
  if [[ -x "$VENV/bin/python" ]]; then
    PYTHON_BIN="$VENV/bin/python"
  else
    for candidate in python3.12 python3.13 python3.11 python3.10 python3; do
      if command -v "$candidate" >/dev/null 2>&1; then
        BASE_PYTHON="$(command -v "$candidate")"
        break
      fi
    done
    [[ -n "$BASE_PYTHON" ]] || die "Python 3.11 or newer is required."
    printf 'Creating virtual environment in %s\n' "$VENV"
    "$BASE_PYTHON" -m venv "$VENV" || die "could not create $VENV; install the python3-venv package"
    PYTHON_BIN="$VENV/bin/python"
    FORCE_INSTALL=1
  fi
fi

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11 or newer is required: $PYTHON_BIN"

deps_ready() {
  "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import importlib
for name in ('numpy', 'cv2', 'PIL', 'reportlab', 'pypdf', 'scipy', 'packaging', 'charset_normalizer'):
    importlib.import_module(name)
PY
}

if ((FORCE_INSTALL)) || ! deps_ready; then
  if [[ "${ANODIZING_OFFLINE:-0}" == '1' ]]; then
    die "Python dependencies are missing and ANODIZING_OFFLINE=1. Install with:
  $PYTHON_BIN -m pip install -r $BUNDLE/app/requirements.txt"
  fi
  printf 'Installing Python dependencies (first run only)...\n'
  "$PYTHON_BIN" -m pip install --disable-pip-version-check -r "$BUNDLE/app/requirements.txt"
  if ((INSTALL_HARDWARE)); then
    printf 'Installing optional camera/robot libraries...\n'
    "$PYTHON_BIN" -m pip install --disable-pip-version-check -r "$BUNDLE/app/requirements-hardware.txt"
  fi
fi

mkdir -p "$DATA_DIR/boards"
cp -a --update=none "$BUNDLE/data/boards/." "$DATA_DIR/boards/" 2>/dev/null \
  || cp -an "$BUNDLE/data/boards/." "$DATA_DIR/boards/"

printf 'UI:   http://127.0.0.1:%s/\n' "$PORT"
printf 'Data: %s\n' "$DATA_DIR"
printf 'Stop with Ctrl-C.\n'

if [[ "$OPEN_BROWSER" == '1' ]]; then
  URL="http://127.0.0.1:$PORT/"
  (
    sleep 1
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$URL" >/dev/null 2>&1 || true
    elif command -v open >/dev/null 2>&1; then
      open "$URL" >/dev/null 2>&1 || true
    else
      "$PYTHON_BIN" -c 'import sys, webbrowser; webbrowser.open(sys.argv[1])' "$URL" >/dev/null 2>&1 || true
    fi
  ) &
fi

export ANODIZING_PYTHON="$PYTHON_BIN"
export ANODIZING_DATA_DIR="$DATA_DIR"
exec "$BUNDLE/tools/run-ipc.bash" --port "$PORT" ${SERVER_ARGS[@]+"${SERVER_ARGS[@]}"}
