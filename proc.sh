#!/usr/bin/env bash
set -Eeuo pipefail

# One-command server installer for GraxStore.
# Run from the project directory:
#   bash proc.sh
#
# The script creates .env/.venv when needed and installs graxstore.service.
# The service runs bot.py directly; systemd keeps it alive across crashes and
# server reboots.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
VENV_DIR="${PROJECT_DIR}/.venv"
SERVICE_NAME="graxstore.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

die() {
  echo "Ошибка: $*" >&2
  exit 1
}

load_env() {
  if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "$ENV_FILE"
    set +a
  fi
}

write_initial_env() {
  load_env
  if [[ -z "${BOT_TOKEN:-}" ]]; then
    read -r -s -p "Введите токен Telegram-бота: " BOT_TOKEN
    echo
  fi
  [[ -n "${BOT_TOKEN:-}" ]] || die "токен Telegram не может быть пустым"

  if [[ -z "${ADMIN_IDS:-}" ]]; then
    read -r -p "Количество администраторов: " ADMIN_COUNT
    [[ "$ADMIN_COUNT" =~ ^[0-9]+$ ]] || die "количество администраторов должно быть числом"
    admin_ids=()
    for ((index = 1; index <= ADMIN_COUNT; index++)); do
      read -r -p "Telegram ID администратора ${index}: " admin_id
      [[ "$admin_id" =~ ^-?[0-9]+$ ]] || die "ID администратора должен быть числом"
      admin_ids+=("$admin_id")
    done
    ADMIN_IDS="$(IFS=,; echo "${admin_ids[*]}")"
  fi
  [[ -n "${ADMIN_IDS:-}" ]] || die "нужен хотя бы один ID администратора"

  umask 077
  {
    printf 'BOT_TOKEN=%s\n' "$BOT_TOKEN"
    printf 'ADMIN_IDS=%s\n' "$ADMIN_IDS"
    printf 'SUPREME_URL=%s\n' "${SUPREME_URL:-https://supreme.com/}"
    printf 'NIKE_URL=%s\n' "${NIKE_URL:-https://www.nike.com/w/mens-lifestyle-shoes-13jrmznik1zy7ok}"
    printf 'ZARA_URL=%s\n' "${ZARA_URL:-https://www.zara.com/us/en/man-new-in-l711.html?v1=2732942}"
    printf 'CATALOG_REFRESH_HOURS=%s\n' "${CATALOG_REFRESH_HOURS:-6}"
    printf 'PAYMENT_BANK_NAME=%s\n' "${PAYMENT_BANK_NAME:-}"
    printf 'PAYMENT_CARD_NUMBER=%s\n' "${PAYMENT_CARD_NUMBER:-}"
    printf 'PAYMENT_RECIPIENT=%s\n' "${PAYMENT_RECIPIENT:-}"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

prepare_python() {
  local python_bin="${PYTHON_BIN:-python3}"
  command -v "$python_bin" >/dev/null 2>&1 || die "не найден Python 3"

  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "Создаю виртуальное окружение..."
    "$python_bin" -m venv "$VENV_DIR" ||
      die "не удалось создать .venv. Установите пакет python3-venv."
  fi

  if ! "${VENV_DIR}/bin/python" -c 'import aiogram, aiohttp, bs4' >/dev/null 2>&1; then
    echo "Устанавливаю зависимости..."
    "${VENV_DIR}/bin/python" -m pip install --upgrade pip
    "${VENV_DIR}/bin/python" -m pip install --upgrade \
      "aiogram>=3.7,<4" \
      "aiohttp>=3.9" \
      "beautifulsoup4>=4.12" \
      "Pillow>=10.0"
  fi
}

detect_service_user() {
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    SERVICE_USER="$SUDO_USER"
  else
    SERVICE_USER="$(stat -c '%U' "$ENV_FILE" 2>/dev/null || id -un)"
    [[ "$SERVICE_USER" != "UNKNOWN" ]] || SERVICE_USER="$(id -un)"
  fi
  SERVICE_GROUP="$(id -gn "$SERVICE_USER")" ||
    die "не удалось определить группу пользователя ${SERVICE_USER}"
}

install_service() {
  [[ "${EUID}" -eq 0 ]] || die "внутренняя установка сервиса должна выполняться от root"
  command -v systemctl >/dev/null 2>&1 ||
    die "на сервере не найден systemd/systemctl"
  [[ -f "$ENV_FILE" ]] || die "не найден .env"
  [[ -x "${VENV_DIR}/bin/python" ]] || die "не найден .venv/bin/python"

  detect_service_user
  cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=GraxStore Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_DIR}/bin/python ${PROJECT_DIR}/bot.py
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
  echo
  echo "Готово: бот добавлен в процессы сервера."
  echo "Сервис: ${SERVICE_NAME}"
  echo "Статус: sudo systemctl status ${SERVICE_NAME}"
  echo "Логи:   sudo journalctl -u ${SERVICE_NAME} -f"
}

if [[ "${1:-}" == "--install-service" ]]; then
  load_env
  prepare_python
  install_service
  exit 0
fi

cd "$PROJECT_DIR"
write_initial_env
prepare_python

if [[ "${EUID}" -eq 0 ]]; then
  install_service
elif command -v sudo >/dev/null 2>&1; then
  echo "Для регистрации systemd-сервиса нужен sudo..."
  sudo bash "$0" --install-service
else
  die "не найден sudo. Запустите bash proc.sh от root."
fi