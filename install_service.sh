#!/usr/bin/env bash
set -Eeuo pipefail

# Installs a persistent systemd service for the bot.
# Run once after creating .env:
#   bash start.sh
#   sudo bash install_service.sh

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="graxstore.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "$RUN_USER")"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите установщик через sudo: sudo bash install_service.sh" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd не найден. Этот установщик предназначен для Linux-сервера с systemd." >&2
  exit 1
fi
if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
  echo "Не найден ${PROJECT_DIR}/.env." >&2
  echo "Сначала один раз выполните: bash start.sh" >&2
  exit 1
fi
if [[ ! -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  echo "Не найдено виртуальное окружение. Сначала выполните: bash start.sh" >&2
  exit 1
fi

cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=GraxStore Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${PROJECT_DIR}
ExecStart=/usr/bin/env bash ${PROJECT_DIR}/start.sh
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo
echo "Сервис установлен и запущен: ${SERVICE_NAME}"
echo "Статус:  sudo systemctl status ${SERVICE_NAME}"
echo "Логи:    sudo journalctl -u ${SERVICE_NAME} -f"