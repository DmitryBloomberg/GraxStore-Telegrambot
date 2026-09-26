#!/usr/bin/env bash
set -Eeuo pipefail

# Backward-compatible alias. The canonical command is: bash proc.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/proc.sh" "$@"