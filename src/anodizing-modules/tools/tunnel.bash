#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 1 ]]; then
  echo "Usage: $0 USER@IPC_HOST" >&2
  exit 2
fi
exec ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8870:127.0.0.1:8870 "$1"
