#!/usr/bin/env bash
set -Eeuo pipefail

# Keep the historical nested path compatible with the repository root.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/start.sh"