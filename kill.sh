#!/usr/bin/env bash
# Stop the anodizing IPC service started by start-ui.sh.
set -euo pipefail

PORT="${ANODIZING_PORT:-8870}"
ALL=0

usage() {
  cat <<EOF
Usage: ./kill.sh [PORT] [--all]

Stops the anodizing IPC service.

  PORT       Service port (default: 8870, env ANODIZING_PORT)
  --port N   Same as the positional PORT
  --all      Stop every anodizing_web.server process, regardless of port
  -h, --help Show this help
EOF
}

while (($#)); do
  case "$1" in
    --port)
      (($# >= 2)) || { echo "error: --port needs a value" >&2; exit 2; }
      PORT="$2"
      shift 2
      ;;
    --port=*)
      PORT="${1#*=}"
      shift
      ;;
    --all)
      ALL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      PORT="$1"
      shift
      ;;
  esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "error: invalid port: $PORT" >&2; exit 2; }
((PORT >= 1 && PORT <= 65535)) || { echo "error: port must be 1 to 65535" >&2; exit 2; }

pids=()
if ((ALL)); then
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(pgrep -f -- "anodizing_web.server" 2>/dev/null || true)
else
  # Prefer the listening port; fall back to matching the command line.
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] && pids+=("$pid")
  done < <(fuser -n tcp "$PORT" 2>/dev/null | tr -s ' ' '\n' || true)
  if ((${#pids[@]} == 0)); then
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && pids+=("$pid")
    done < <(pgrep -f -- "anodizing_web.server.*--port[ =]${PORT}([^0-9]|$)" 2>/dev/null || true)
  fi
fi

valid=()
for pid in "${pids[@]}"; do
  [[ "$pid" == "$$" ]] && continue
  command="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  if [[ "$command" == *anodizing_web.server* ]]; then
    valid+=("$pid")
  elif [[ -n "$command" ]]; then
    printf 'Skipping PID %s; not the anodizing service:\n  %s\n' "$pid" "$command" >&2
  fi
done

if ((${#valid[@]} == 0)); then
  printf 'No anodizing service found on port %s.\n' "$PORT"
  exit 0
fi

if ((ALL)); then
  printf 'Stopping all anodizing services (PID %s)...\n' "${valid[*]}"
else
  printf 'Stopping anodizing service on port %s (PID %s)...\n' "$PORT" "${valid[*]}"
fi
kill -INT "${valid[@]}" 2>/dev/null || true

for _ in {1..40}; do
  alive=0
  for pid in "${valid[@]}"; do
    kill -0 "$pid" 2>/dev/null && alive=1
  done
  ((alive)) || { echo 'Stopped.'; exit 0; }
  sleep 0.25
done

printf 'Still running; sending SIGTERM...\n'
kill -TERM "${valid[@]}" 2>/dev/null || true
for _ in {1..40}; do
  alive=0
  for pid in "${valid[@]}"; do
    kill -0 "$pid" 2>/dev/null && alive=1
  done
  ((alive)) || { echo 'Stopped.'; exit 0; }
  sleep 0.25
done

printf 'Still running; sending SIGKILL...\n'
kill -KILL "${valid[@]}" 2>/dev/null || true
sleep 0.5
for pid in "${valid[@]}"; do
  if kill -0 "$pid" 2>/dev/null; then
    echo "error: PID $pid is still alive" >&2
    exit 1
  fi
done
echo 'Stopped.'
