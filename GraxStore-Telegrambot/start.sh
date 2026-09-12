#!/usr/bin/env bash
set -Eeuo pipefail

# One-command installer and launcher for GraxStore.
# The bot itself is intentionally kept in bot.py; this script only prepares
# the isolated environment and writes the runtime settings.

cd "$(dirname "${BASH_SOURCE[0]}")"
ENV_FILE=".env"

if [[ -z "${BOT_TOKEN:-}" && -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

if [[ -z "${BOT_TOKEN:-}" ]]; then
  read -r -s -p "Введите токен Telegram-бота: " BOT_TOKEN
  echo
fi
if [[ -z "$BOT_TOKEN" ]]; then
  echo "Токен не может быть пустым." >&2
  exit 1
fi

if [[ -z "${ADMIN_IDS:-}" ]]; then
  read -r -p "Количество администраторов: " ADMIN_COUNT
  [[ "$ADMIN_COUNT" =~ ^[0-9]+$ ]] || { echo "Нужно число."; exit 1; }
  ADMIN_IDS_ARRAY=()
  for ((i=1; i<=ADMIN_COUNT; i++)); do
    read -r -p "Telegram ID администратора $i: " ADMIN_ID
    [[ "$ADMIN_ID" =~ ^-?[0-9]+$ ]] || { echo "ID должен быть числом."; exit 1; }
    ADMIN_IDS_ARRAY+=("$ADMIN_ID")
  done
  ADMIN_IDS="$(IFS=,; echo "${ADMIN_IDS_ARRAY[*]}")"
fi

umask 077
cat > "$ENV_FILE" <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
SUPREME_URL=${SUPREME_URL:-https://supreme.com/}
PAYMENT_BANK_NAME=${PAYMENT_BANK_NAME:-}
PAYMENT_CARD_NUMBER=${PAYMENT_CARD_NUMBER:-}
PAYMENT_RECIPIENT=${PAYMENT_RECIPIENT:-}
EOF
chmod 600 "$ENV_FILE"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Не найден Python 3." >&2
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  echo "Создаю виртуальное окружение…"
  "$PYTHON_BIN" -m venv .venv
fi

# All runtime libraries are declared here, so a fresh server needs no manual
# requirements.txt setup.
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --upgrade \
  "aiogram>=3.7,<4" \
  "aiohttp>=3.9" \
  "beautifulsoup4>=4.12" \
  "Pillow>=10.0"

exec .venv/bin/python bot.py