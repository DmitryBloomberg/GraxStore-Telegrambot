#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="graxstore.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите через sudo: sudo bash uninstall_service.sh" >&2
  exit 1
fi

systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}"
systemctl daemon-reload
echo "Сервис ${SERVICE_NAME} удалён."