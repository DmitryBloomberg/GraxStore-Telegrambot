#!/usr/bin/env bash
set -Eeuo pipefail

# Compatibility entry point: configure the bot and install its systemd service.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/proc.sh" "$@"
