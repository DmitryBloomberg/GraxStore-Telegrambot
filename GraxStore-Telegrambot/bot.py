"""GraxStore Telegram shop.

The application intentionally lives in one file.  It uses JSON files as a
small, transparent storage layer, so the shop can be backed up without a
database and remains easy to move to another server.
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from bs4 import BeautifulSoup

try:
    from PIL import Image
except ImportError:  # start.sh installs Pillow; this keeps imports friendly in tests
    Image = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
USERS_DIR = DATA_DIR / "users"
ORDERS_DIR = DATA_DIR / "orders"
PRODUCTS_DIR = DATA_DIR / "products"
SETTINGS_PATH = DATA_DIR / "settings.json"
ENV_PATH = BASE_DIR / ".env"

ORDER_STATUSES = (
    "Оформлено",
    "Ожидает отправки в Россию",
    "Доставка на склад",
    "Готово к выдаче",
    "Выдано",
)
ORDER_FROZEN_STATUS = "Заморозка"
LEGACY_ORDER_STATUS_MAP = {
    "Оформлен": "Оформлено",
    "Покупка": "Ожидает отправки в Россию",
    "Отправлено в Россию": "Доставка на склад",
}
STORE_CLOSED_MESSAGE = (
    "В данный момент магазин закрыт, следите за открытием в нашем телеграмм канале "
    "https://t.me/grax78"
)
PAYMENT_STATUS_LABELS = {
    "awaiting_receipt": "Ожидается чек об оплате",
    "receipt_uploaded": "Чек на проверке",
    "approved": "Оплата подтверждена",
    "rejected": "Оплата отклонена",
}
DEFAULT_SETTINGS = {
    "store_open": True,
    "delivery_fee_rub": 0,
    "markup_rub": 2000,
    "usd_rub_rate": 100.0,
    "terms_url": "https://t.me/grax45",
    "policy_url": "https://t.me/grax45",
    "payment_bank_name": os.getenv("PAYMENT_BANK_NAME", ""),
    "payment_card_number": os.getenv("PAYMENT_CARD_NUMBER", ""),
    "payment_recipient": os.getenv("PAYMENT_RECIPIENT", ""),
}


def load_dotenv() -> None:
    """Load the tiny .env written by start.sh without another dependency."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("graxstore")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPREME_URL = os.getenv("SUPREME_URL", "https://supreme.com/").strip()
ADMIN_IDS = {
    int(value)
    for value in os.getenv("ADMIN_IDS", "").replace(";", ",").split(",")
    if value.strip().lstrip("-").isdigit()
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for path in (DATA_DIR, USERS_DIR, ORDERS_DIR, PRODUCTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def settings() -> dict[str, Any]:
    current = DEFAULT_SETTINGS.copy()
    saved = read_json(SETTINGS_PATH, {})
    if isinstance(saved, dict):
        current.update(saved)
    return current


def user_path(user_id: int) -> Path:
    return USERS_DIR / f"{user_id}.json"


def get_user(user_id: int) -> dict[str, Any]:
    data = read_json(user_path(user_id), {})
    return data if isinstance(data, dict) else {}


def save_user(user: dict[str, Any]) -> None:
    write_json(user_path(int(user["id"])), user)


def ensure_user(tg_user: Any) -> dict[str, Any]:
    user = get_user(tg_user.id)
    if not user:
        user = {
            "id": tg_user.id,
            "first_name": tg_user.first_name or "",
            "last_name": tg_user.last_name or "",
            "username": tg_user.username or "",
            "blocked": False,
            "cart": [],
            "created_at": now_iso(),
        }
    else:
        user.update(
            {
                "first_name": tg_user.first_name or "",
                "last_name": tg_user.last_name or "",
                "username": tg_user.username or "",
            }
        )
    user["updated_at"] = now_iso()
    user.setdefault("cart", [])
    user.setdefault("blocked", False)
    save_user(user)
    return user


def product_path(product_id: str | int) -> Path:
    return PRODUCTS_DIR / str(product_id)


def load_product(product_id: str | int) -> dict[str, Any] | None:
    path = product_path(product_id) / "price.json"
    item = read_json(path, None)
    return item if isinstance(item, dict) else None


def load_products() -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    if not PRODUCTS_DIR.exists():
        return products
    for directory in sorted(
        (path for path in PRODUCTS_DIR.iterdir() if path.is_dir() and path.name.isdigit()),
        key=lambda path: int(path.name),
    ):
        item = load_product(directory.name)
        if item:
            item["id"] = directory.name
            products.append(item)
    return products


def reprice_catalog(delivery_fee_rub: float) -> int:
    """Apply a new delivery fee to products that have not been ordered yet.

    Orders contain a full price snapshot, so changing the catalog never
    changes the amount already recorded in an order.
    """
    changed = 0
    for directory in PRODUCTS_DIR.iterdir() if PRODUCTS_DIR.exists() else []:
        if not directory.is_dir() or not directory.name.isdigit():
            continue
        path = directory / "price.json"
        product = read_json(path, None)
        if not isinstance(product, dict):
            continue
        product["delivery_fee_rub"] = round(delivery_fee_rub)
        product["price_with_delivery_rub"] = round(
            float(product.get("price_rub", 0))
            + float(delivery_fee_rub)
            + float(product.get("markup_rub", 2000))
        )
        product["updated_at"] = now_iso()
        write_json(path, product)
        changed += 1
    return changed


def money(value: float | int) -> str:
    return f"{float(value):,.0f}".replace(",", " ") + " ₽"


def canonical_order_status(status: Any) -> str:
    value = str(status or ORDER_STATUSES[0])
    return LEGACY_ORDER_STATUS_MAP.get(value, value)


def usd(value: float | int) -> str:
    return f"${float(value):,.2f}"


def product_caption(product: dict[str, Any]) -> str:
    return (
        f"<b>{html.escape(str(product.get('name', 'Товар')))}</b>\n"
        f"Цена в магазине: <b>{usd(product.get('price_usd', 0))}</b>\n"
        f"Цена в рублях: <b>{money(product.get('price_rub', 0))}</b>\n"
        f"Итого с доставкой: <b>{money(product.get('price_with_delivery_rub', 0))}</b>"
    )


def product_keyboard(product_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Добавить в корзину",
                    callback_data=f"cart_add:{product_id}",
                )
            ]
        ]
    )


def cart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оформить заказ", callback_data="cart_order")],
            [InlineKeyboardButton(text="Очистить корзину", callback_data="cart_clear")],
        ]
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Условия доставки", url=settings()["terms_url"]),
                InlineKeyboardButton(text="Политика сервиса", url=settings()["policy_url"]),
            ],
            [InlineKeyboardButton(text="Товары", callback_data="show_products")],
            [InlineKeyboardButton(text="Максимальный бюджет", callback_data="budget_start")],
            [InlineKeyboardButton(text="Корзина", callback_data="show_cart")],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    store_is_open = bool(settings().get("store_open", True))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Активные заказы", callback_data="admin_orders"),
            ],
            [
                InlineKeyboardButton(text="Пользователи ZIP", callback_data="admin_export"),
                InlineKeyboardButton(text="Внести пользователей ZIP", callback_data="admin_import"),
            ],
            [
                InlineKeyboardButton(
                    text="Изменить цену доставки", callback_data="admin_delivery"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Закрыть магазин" if store_is_open else "Открыть магазин",
                    callback_data="admin_toggle",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Переустановить позиции товаров",
                    callback_data="admin_refresh",
                ),
            ],
            [
                InlineKeyboardButton(text="Проверить оплаты", callback_data="admin_payments"),
                InlineKeyboardButton(text="Статистика", callback_data="admin_stats"),
            ],
            [
                InlineKeyboardButton(text="Пользователи", callback_data="admin_users"),
                InlineKeyboardButton(
                    text="Реквизиты оплаты", callback_data="admin_payment_settings"
                ),
            ],
            [InlineKeyboardButton(text="Удалить заказ", callback_data="admin_delete")],
        ]
    )


def admin_order_keyboard(order: dict[str, Any]) -> InlineKeyboardMarkup:
    """Controls shown under every order in the administrator's order list."""
    order_id = str(order["id"])
    user_id = str(order.get("user_id", ""))
    frozen = order.get("status") == ORDER_FROZEN_STATUS
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Написать", url=f"tg://user?id={user_id}"),
                InlineKeyboardButton(
                    text="Изменить этап",
                    callback_data=f"admin_stage:{order_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Откатить этап",
                    callback_data=f"admin_rollback:{order_id}",
                ),
                InlineKeyboardButton(
                    text="Разморозить" if frozen else "Заморозить",
                    callback_data=f"admin_freeze:{order_id}",
                ),
            ],
        ]
    )


def admin_stage_keyboard(order_id: str, frozen: bool = False) -> InlineKeyboardMarkup:
    statuses = ORDER_STATUSES[:1] if frozen else ORDER_STATUSES
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for index, status in enumerate(statuses):
        row.append(
            InlineKeyboardButton(
                text=status,
                callback_data=f"admin_stage_set:{order_id}:{index}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_review_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить",
                    callback_data=f"payment:approve:{order_id}",
                ),
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=f"payment:reject:{order_id}",
                ),
            ]
        ]
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_blocked(user_id: int) -> bool:
    return bool(get_user(user_id).get("blocked", False))


def order_files() -> list[Path]:
    return sorted(
        (path for path in ORDERS_DIR.iterdir() if path.is_dir() and path.name.isdigit()),
        key=lambda path: int(path.name),
    )


def next_order_id() -> str:
    ids = [int(path.name) for path in order_files()]
    return str(max(ids, default=0) + 1)


def load_order(order_id: str | int) -> dict[str, Any] | None:
    order = read_json(ORDERS_DIR / str(order_id) / "order.json", None)
    return order if isinstance(order, dict) else None


def find_photo(product: dict[str, Any]) -> Path | None:
    directory = product_path(product["id"])
    for filename in product.get("images", []):
        local = directory / str(filename)
        if local.exists():
            return local
    return next(iter(sorted(directory.glob("*.jpeg"))), None)


async def send_product(bot: Bot, chat_id: int, product: dict[str, Any]) -> None:
    photo = find_photo(product)
    caption = product_caption(product)
    keyboard = product_keyboard(str(product["id"]))
    try:
        if photo:
            await bot.send_photo(
                chat_id,
                FSInputFile(photo),
                caption=caption,
                reply_markup=keyboard,
            )
        elif product.get("source_image"):
            await bot.send_photo(
                chat_id,
                str(product["source_image"]),
                caption=caption,
                reply_markup=keyboard,
            )
        else:
            await bot.send_message(chat_id, caption, reply_markup=keyboard)
    except Exception:
        log.exception("Could not send product %s", product.get("id"))
        await bot.send_message(chat_id, caption, reply_markup=keyboard)


def catalog_pagination_keyboard(
    next_offset: int, total: int, budget: int | None = None
) -> InlineKeyboardMarkup | None:
    if next_offset >= total:
        return None
    callback_data = (
        f"budget_page:{budget}:{next_offset}"
        if budget is not None
        else f"products_page:{next_offset}"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Показать еще", callback_data=callback_data)]
        ]
    )


async def show_products(
    bot: Bot, chat_id: int, offset: int = 0, budget: int | None = None
) -> None:
    if not settings().get("store_open", True):
        await bot.send_message(chat_id, STORE_CLOSED_MESSAGE)
        return
    all_products = load_products()
    products = (
        [
            product
            for product in all_products
            if float(product.get("price_with_delivery_rub", 0)) <= budget
        ]
        if budget is not None
        else all_products
    )
    if not products:
        await bot.send_message(
            chat_id,
            (
                f"До {money(budget)} подходящих товаров не найдено."
                if budget is not None
                else "Сейчас товары не загружены. Администратор может обновить каталог."
            ),
        )
        return
    page = products[offset : offset + 5]
    if budget is None:
        title = f"Товары {offset + 1}–{min(offset + 5, len(products))} из {len(products)}"
    else:
        title = (
            f"Товары до {money(budget)}: "
            f"{offset + 1}–{min(offset + 5, len(products))} из {len(products)}"
        )
    await bot.send_message(chat_id, title)
    for product in page:
        await send_product(bot, chat_id, product)
        await asyncio.sleep(0.08)
    keyboard = catalog_pagination_keyboard(offset + 5, len(products), budget)
    if keyboard:
        await bot.send_message(chat_id, "Показать следующие 5 позиций:", reply_markup=keyboard)


async def show_cart(bot: Bot, chat_id: int, user_id: int) -> None:
    user = get_user(user_id)
    cart_ids = [str(value) for value in user.get("cart", [])]
    products = [load_product(value) | {"id": value} for value in cart_ids if load_product(value)]
    if not products:
        await bot.send_message(chat_id, "Корзина пока пуста.")
        return
    total = sum(float(item.get("price_with_delivery_rub", 0)) for item in products)
    await bot.send_message(
        chat_id,
        "В корзине:\n"
        + "\n".join(
            f"• {html.escape(str(item.get('name', 'Товар')))} — "
            f"{money(item.get('price_with_delivery_rub', 0))}"
            for item in products
        )
        + f"\n\n<b>Итого: {money(total)}</b>",
        reply_markup=cart_keyboard(),
    )
    for product in products:
        photo = find_photo(product)
        if photo:
            await bot.send_photo(
                chat_id,
                FSInputFile(photo),
                caption=html.escape(str(product.get("name", "Товар"))),
            )


def parse_price(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def normalize_images(images: Any, base_url: str) -> list[str]:
    if isinstance(images, str):
        images = [images]
    if not isinstance(images, list):
        return []
    result = []
    for image in images:
        if isinstance(image, dict):
            image = image.get("src") or image.get("url")
        if not image:
            continue
        url = urljoin(base_url, str(image).split("?")[0])
        if url not in result:
            result.append(url)
    return result


def append_product(
    result: list[dict[str, Any]],
    item: Any,
    base_url: str,
    source_currency: str = "USD",
) -> None:
    if not isinstance(item, dict):
        return
    title = item.get("title") or item.get("name") or item.get("product_name")
    if not title:
        return
    offers = item.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = (
        item.get("price")
        or item.get("price_usd")
        or (offers.get("price") if isinstance(offers, dict) else None)
        or (item.get("variants", [{}])[0].get("price") if item.get("variants") else None)
    )
    price_value = parse_price(price)
    if price_value is None:
        return
    # Supreme's current embedded catalog uses integer minor units while the
    # older Shopify endpoint returns strings such as "48.00".
    if isinstance(price, (int, float)) and abs(float(price)) >= 10000:
        price_value /= 100
    images = normalize_images(
        item.get("images") or item.get("image") or item.get("image_url"),
        base_url,
    )
    handle = item.get("handle")
    url = item.get("url") or item.get("product_url")
    if not url and handle:
        url = urljoin(base_url, f"/products/{handle}")
    url = urljoin(base_url, str(url or ""))
    result.append(
        {
            "name": str(title).strip(),
            "price_usd": price_value,
            "price_source": price_value,
            "source_currency": source_currency.upper(),
            "images": images,
            "source_url": url,
        }
    )


def parse_products_html(text: str, base_url: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    soup = BeautifulSoup(text, "html.parser")
    source_currency = "USD"
    currency_match = re.search(
        r'Shopify\.currency\s*=\s*\{"active"\s*:\s*"([A-Z]{3})"',
        text,
    )
    if currency_match:
        source_currency = currency_match.group(1)
    else:
        currency_match = re.search(
            r'"paymentSettings"\s*:\s*\{"currencyCode"\s*:\s*"([A-Z]{3})"',
            text,
        )
        if currency_match:
            source_currency = currency_match.group(1)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict) and "@graph" in value:
                values.extend(value["@graph"])
            append_product(result, value, base_url, source_currency)

    for script in soup.select("script#__NEXT_DATA__, script[type='application/json']"):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if any(key in value for key in ("title", "name")) and any(
                    key in value for key in ("price", "variants", "offers")
                ):
                    append_product(result, value, base_url, source_currency)
                for nested in value.values():
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(payload)

    # Last-resort extraction for catalog cards in ordinary HTML.
    for link in soup.select("a[href*='/products/']"):
        href = urljoin(base_url, link.get("href", ""))
        title_node = link.select_one("[class*='title'], [class*='name']")
        title = (
            link.get("aria-label")
            or link.get("title")
            or (title_node.get_text(" ", strip=True) if title_node else "")
            or link.get_text(" ", strip=True)
        )
        price_match = re.search(r"\$\s*([0-9]+(?:[.,][0-9]{1,2})?)", link.get_text(" ", strip=True))
        image = link.select_one("img")
        if title and price_match:
            result.append(
                {
                    "name": title.strip(),
                    "price_usd": parse_price(price_match.group(1)),
                    "price_source": parse_price(price_match.group(1)),
                    "source_currency": source_currency,
                    "images": normalize_images(
                        image.get("src") if image else None, base_url
                    ),
                    "source_url": href,
                }
            )

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in result:
        key = item["source_url"] or f"{item['name']}:{item['price_usd']}"
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def parse_shopify_json(payload: Any, base_url: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    values = payload.get("products", []) if isinstance(payload, dict) else []
    for product in values:
        append_product(result, product, base_url, "USD")
    return result


async def fetch_usd_rub(session: aiohttp.ClientSession) -> float:
    fallback = float(settings().get("usd_rub_rate", 100.0))
    for endpoint in (
        "https://open.er-api.com/v6/latest/USD",
        "https://api.frankfurter.app/latest?from=USD&to=RUB",
    ):
        try:
            async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=12)) as response:
                payload = await response.json(content_type=None)
                value = payload.get("rates", {}).get("RUB")
                if value:
                    return float(value)
        except Exception:
            log.warning("Exchange-rate request failed: %s", endpoint)
    return fallback


async def fetch_currency_rates(
    session: aiohttp.ClientSession, currency: str
) -> tuple[float, float]:
    """Return source-currency-to-USD and source-currency-to-RUB rates."""
    if currency.upper() == "USD":
        usd_rate = await fetch_usd_rub(session)
        return 1.0, usd_rate
    try:
        endpoint = f"https://open.er-api.com/v6/latest/{currency.upper()}"
        async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=12)) as response:
            payload = await response.json(content_type=None)
        rates = payload.get("rates", {})
        if rates.get("USD") and rates.get("RUB"):
            return float(rates["USD"]), float(rates["RUB"])
    except Exception:
        log.warning("Could not convert %s prices to USD", currency)
    usd_rub = await fetch_usd_rub(session)
    return 1 / 100.0, usd_rub / 0.01


async def download_as_jpeg(
    session: aiohttp.ClientSession, url: str, destination: Path
) -> bool:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as response:
            if response.status >= 400:
                return False
            content = await response.read()
        if Image is None:
            destination.write_bytes(content)
            return True
        image = Image.open(io.BytesIO(content)).convert("RGB")
        image.thumbnail((2400, 2400))
        image.save(destination, "JPEG", quality=92)
        return True
    except Exception:
        log.warning("Image download failed: %s", url)
        return False


async def scrape_supreme() -> tuple[list[dict[str, Any]], float]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/124 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        products: list[dict[str, Any]] = []
        try:
            async with session.get(SUPREME_URL) as response:
                homepage = await response.text(errors="ignore")
            products = parse_products_html(homepage, SUPREME_URL)
        except Exception:
            log.exception("Supreme homepage request failed")

        # Supreme's frontend has changed several times; Shopify's JSON endpoint
        # is a useful fallback when the visible HTML contains no product cards.
        if not products:
            for endpoint in (
                urljoin(SUPREME_URL, "/pages/shop"),
                urljoin(SUPREME_URL, "/products.json?limit=250"),
                urljoin(SUPREME_URL, "/collections/all/products.json?limit=250"),
            ):
                try:
                    async with session.get(endpoint) as response:
                        content_type = response.headers.get("content-type", "")
                        if "json" in content_type or endpoint.endswith(".json?limit=250"):
                            payload = await response.json(content_type=None)
                            products = parse_shopify_json(payload, SUPREME_URL)
                        else:
                            page = await response.text(errors="ignore")
                            products = parse_products_html(page, endpoint)
                    if products:
                        break
                except Exception:
                    log.warning("Catalog JSON request failed: %s", endpoint)

        currency = products[0].get("source_currency", "USD") if products else "USD"
        source_to_usd, source_to_rub = await fetch_currency_rates(session, currency)
        for product in products:
            product["price_usd"] = round(
                float(product.get("price_source", product.get("price_usd", 0)))
                * source_to_usd,
                2,
            )
        usd_to_rub = source_to_rub / source_to_usd if source_to_usd else 100.0
        return products, usd_to_rub


async def sync_products() -> int:
    products, rate = await scrape_supreme()
    if not products:
        log.warning("Supreme returned no products; existing catalog was preserved")
        return 0

    current_settings = settings()
    current_settings["usd_rub_rate"] = round(rate, 4)
    write_json(SETTINGS_PATH, current_settings)
    staging = Path(tempfile.mkdtemp(prefix="products-", dir=str(DATA_DIR)))
    image_semaphore = asyncio.Semaphore(12)
    try:
        async def build_product(index: int, raw: dict[str, Any]) -> None:
            folder = staging / str(index)
            folder.mkdir()
            price_usd = float(raw["price_usd"])
            price_rub = round(price_usd * rate)
            delivery_fee = float(current_settings.get("delivery_fee_rub", 0))
            markup = float(current_settings.get("markup_rub", 2000))

            async def download_product_image(
                image_number: int, image_url: str
            ) -> str | None:
                filename = f"{image_number}.jpeg"
                async with image_semaphore:
                    downloaded = await download_as_jpeg(
                        session_for_sync, image_url, folder / filename
                    )
                return filename if downloaded else None

            downloaded_images = await asyncio.gather(
                *(
                    download_product_image(image_number, image_url)
                    for image_number, image_url in enumerate(
                        raw.get("images", [])[:8], start=1
                    )
                )
            )
            image_names = [filename for filename in downloaded_images if filename]
            data = {
                "id": str(index),
                "name": raw["name"],
                "price_usd": round(price_usd, 2),
                "price_rub": price_rub,
                "delivery_fee_rub": round(delivery_fee),
                "markup_rub": round(markup),
                "price_with_delivery_rub": round(price_rub + delivery_fee + markup),
                "images": image_names,
                "source_image": (raw.get("images") or [None])[0],
                "source_url": raw.get("source_url", ""),
                "updated_at": now_iso(),
            }
            write_json(folder / "price.json", data)

        await asyncio.gather(
            *(build_product(index, raw) for index, raw in enumerate(products, start=1))
        )
        old = PRODUCTS_DIR.with_name("products-old")
        if old.exists():
            shutil.rmtree(old)
        PRODUCTS_DIR.rename(old)
        staging.rename(PRODUCTS_DIR)
        shutil.rmtree(old)
        return len(products)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# A session is created only during sync and closed at the end.  Keeping this
# tiny holder avoids opening a new TCP session for every image.
session_for_sync: aiohttp.ClientSession


async def sync_products_with_session() -> int:
    global session_for_sync
    headers = {"User-Agent": "GraxStore/1.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        session_for_sync = session
        return await sync_products()


class AdminStates(StatesGroup):
    delivery = State()
    delete_order = State()
    import_users = State()
    payment_bank = State()
    payment_card = State()
    payment_recipient = State()


class UserStates(StatesGroup):
    max_budget = State()


dp = Dispatcher()


async def deny_if_unavailable(message: Message) -> bool:
    user = ensure_user(message.from_user)
    if user.get("blocked"):
        await message.answer("Ваш аккаунт заблокирован. Обратитесь к поддержке.")
        return True
    return False


@dp.message(CommandStart())
async def command_start(message: Message) -> None:
    if await deny_if_unavailable(message):
        return
    if is_admin(message.from_user.id):
        await message.answer("Панель администратора", reply_markup=admin_keyboard())
        return
    await message.answer(
        f"Здравствуйте, {html.escape(message.from_user.first_name or 'гость')}!\n"
        "Добро пожаловать в GraxStore.",
        reply_markup=profile_keyboard(),
    )


@dp.message(Command("help"))
async def command_help(message: Message) -> None:
    if await deny_if_unavailable(message):
        return
    await message.answer(
        "Поддержка доставки: @grax45\n"
        "Поддержка по работе бота: @butovsky_support"
    )


@dp.message(Command("products"))
async def command_products(message: Message) -> None:
    if await deny_if_unavailable(message):
        return
    await show_products(message.bot, message.chat.id)


@dp.callback_query(F.data == "budget_start")
async def budget_start(callback: CallbackQuery, state: FSMContext) -> None:
    if is_blocked(callback.from_user.id):
        await callback.answer("Ваш аккаунт заблокирован", show_alert=True)
        return
    if not settings().get("store_open", True):
        await callback.message.answer(STORE_CLOSED_MESSAGE)
        await callback.answer()
        return
    await state.set_state(UserStates.max_budget)
    await callback.message.answer(
        "Введите максимальный бюджет в рублях, например: 50000"
    )
    await callback.answer()


@dp.message(UserStates.max_budget)
async def receive_max_budget(message: Message, state: FSMContext) -> None:
    if await deny_if_unavailable(message):
        await state.clear()
        return
    value = parse_price(message.text or "")
    if value is None or value < 0:
        await message.answer("Введите положительное число в рублях, например: 50000")
        return
    await state.clear()
    await show_products(message.bot, message.chat.id, budget=round(value))


@dp.callback_query(F.data.startswith("products_page:"))
async def products_page(callback: CallbackQuery) -> None:
    if is_blocked(callback.from_user.id):
        await callback.answer("Ваш аккаунт заблокирован", show_alert=True)
        return
    offset = callback.data.split(":", 1)[1]
    if not offset.isdigit():
        await callback.answer("Некорректная страница", show_alert=True)
        return
    await callback.answer()
    await show_products(callback.bot, callback.message.chat.id, int(offset))


@dp.callback_query(F.data.startswith("budget_page:"))
async def budget_page(callback: CallbackQuery) -> None:
    if is_blocked(callback.from_user.id):
        await callback.answer("Ваш аккаунт заблокирован", show_alert=True)
        return
    _, budget, offset = callback.data.split(":")
    if not budget.isdigit() or not offset.isdigit():
        await callback.answer("Некорректная страница", show_alert=True)
        return
    await callback.answer()
    await show_products(
        callback.bot,
        callback.message.chat.id,
        int(offset),
        int(budget),
    )


@dp.message(Command("orders"))
async def command_orders(message: Message) -> None:
    if await deny_if_unavailable(message):
        return
    orders = [
        load_order(path.name)
        for path in order_files()
        if load_order(path.name)
    ]
    own = [order for order in orders if order.get("user_id") == message.from_user.id]
    if not own:
        await message.answer("У вас пока нет заказов.")
        return
    lines = ["<b>Ваши заказы:</b>"]
    for order in reversed(own):
        payment_status = order.get("payment_status", "approved")
        lines.append(
            f"№{order['id']} — {html.escape(canonical_order_status(order.get('status')))}\n"
            f"Оплата: {html.escape(PAYMENT_STATUS_LABELS.get(payment_status, payment_status))}\n"
            f"{money(order.get('total_rub', 0))}\n"
            + "\n".join(f"• {html.escape(item['name'])}" for item in order.get("items", []))
        )
    await message.answer("\n\n".join(lines))


@dp.callback_query(F.data == "show_products")
async def callback_products(callback: CallbackQuery) -> None:
    await callback.answer()
    if is_blocked(callback.from_user.id):
        await callback.message.answer("Ваш аккаунт заблокирован.")
        return
    if not settings().get("store_open", True):
        await callback.message.answer(STORE_CLOSED_MESSAGE)
        return
    await show_products(callback.bot, callback.message.chat.id)


@dp.callback_query(F.data == "show_cart")
async def callback_cart(callback: CallbackQuery) -> None:
    await callback.answer()
    if is_blocked(callback.from_user.id):
        await callback.message.answer("Ваш аккаунт заблокирован.")
        return
    await show_cart(callback.bot, callback.message.chat.id, callback.from_user.id)


@dp.callback_query(F.data.startswith("cart_add:"))
async def callback_cart_add(callback: CallbackQuery) -> None:
    if is_blocked(callback.from_user.id):
        await callback.answer("Аккаунт заблокирован", show_alert=True)
        return
    product_id = callback.data.split(":", 1)[1]
    product = load_product(product_id)
    current_settings = settings()
    if not product:
        await callback.answer("Товар уже убран из каталога", show_alert=True)
        return
    if not current_settings.get("store_open", True):
        await callback.answer("Магазин временно закрыт", show_alert=True)
        return
    user = ensure_user(callback.from_user)
    cart = [str(value) for value in user.get("cart", [])]
    if product_id not in cart:
        cart.append(product_id)
        user["cart"] = cart
        save_user(user)
    await callback.answer("Товар добавлен в корзину")


@dp.callback_query(F.data == "cart_clear")
async def callback_cart_clear(callback: CallbackQuery) -> None:
    user = ensure_user(callback.from_user)
    user["cart"] = []
    save_user(user)
    await callback.answer("Корзина очищена")
    await callback.message.answer("Корзина очищена.")


@dp.callback_query(F.data == "cart_order")
async def callback_cart_order(callback: CallbackQuery) -> None:
    if is_blocked(callback.from_user.id):
        await callback.answer("Аккаунт заблокирован", show_alert=True)
        return
    if not settings().get("store_open", True):
        await callback.answer("Магазин временно закрыт", show_alert=True)
        return
    user = ensure_user(callback.from_user)
    products = [
        load_product(value)
        for value in user.get("cart", [])
        if load_product(value)
    ]
    if not products:
        await callback.answer("Корзина пуста", show_alert=True)
        return
    order_id = next_order_id()
    items = [
        {
            "product_id": product.get("id"),
            "name": product.get("name", "Товар"),
            "price_usd": product.get("price_usd", 0),
            "price_rub": product.get("price_rub", 0),
            "delivery_fee_rub": product.get("delivery_fee_rub", 0),
            "markup_rub": product.get("markup_rub", 2000),
            "price_with_delivery_rub": product.get("price_with_delivery_rub", 0),
            "source_url": product.get("source_url", ""),
        }
        for product in products
    ]
    order = {
        "id": order_id,
        "user_id": callback.from_user.id,
        "customer": {
            "first_name": callback.from_user.first_name or "",
            "last_name": callback.from_user.last_name or "",
            "username": callback.from_user.username or "",
        },
        "items": items,
        "total_rub": round(sum(float(item["price_with_delivery_rub"]) for item in items)),
        "status": ORDER_STATUSES[0],
        "payment_status": "awaiting_receipt",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    order_dir = ORDERS_DIR / order_id
    order_dir.mkdir(parents=True, exist_ok=True)
    write_json(order_dir / "order.json", order)
    user["cart"] = []
    save_user(user)
    payment = settings()
    bank_name = str(payment.get("payment_bank_name", "")).strip()
    card_number = str(payment.get("payment_card_number", "")).strip()
    recipient = str(payment.get("payment_recipient", "")).strip()
    if bank_name and card_number:
        requisites = (
            f"Банк: <b>{html.escape(bank_name)}</b>\n"
            f"Карта: <code>{html.escape(card_number)}</code>\n"
            + (f"Получатель: {html.escape(recipient)}\n" if recipient else "")
        )
    else:
        requisites = (
            "Реквизиты пока не настроены администратором. "
            "Обратитесь в поддержку магазина.\n"
        )
    await callback.answer("Заявка оформлена")
    await callback.message.answer(
        f"<b>Заявка на заказ №{order_id}</b>\n"
        f"Сумма к переводу: <b>{money(order['total_rub'])}</b>\n\n"
        f"{requisites}\n"
        "После перевода отправьте сюда чек об оплате документом или фотографией.\n"
        f"В подписи к чеку можно указать номер заявки: <b>№{order_id}</b>."
    )


def payment_order_for_user(user_id: int, requested_id: str | None = None) -> dict[str, Any] | None:
    if requested_id and requested_id.isdigit():
        order = load_order(requested_id)
        if (
            order
            and order.get("user_id") == user_id
            and order.get("payment_status", "approved") in {"awaiting_receipt", "rejected"}
        ):
            return order
        return None
    candidates = []
    for path in order_files():
        order = load_order(path.name)
        if (
            order
            and order.get("user_id") == user_id
            and order.get("payment_status", "approved") in {"awaiting_receipt", "rejected"}
        ):
            candidates.append(order)
    return candidates[-1] if candidates else None


async def process_receipt(
    message: Message, file_id: str, file_type: str, file_name: str = ""
) -> None:
    if await deny_if_unavailable(message):
        return
    requested_id_match = re.search(r"(?:№|#|заказ(?:а)?\s*)\s*(\d+)", message.caption or "", re.I)
    requested_id = requested_id_match.group(1) if requested_id_match else None
    order = payment_order_for_user(message.from_user.id, requested_id)
    if not order:
        await message.answer(
            "Не нашёл заявку, ожидающую чек. Сначала оформите заказ через корзину "
            "или укажите номер заявки в подписи к чеку."
        )
        return
    order["payment_status"] = "receipt_uploaded"
    order["receipt_file_id"] = file_id
    order["receipt_file_type"] = file_type
    order["receipt_file_name"] = file_name
    order["receipt_caption"] = message.caption or ""
    order["updated_at"] = now_iso()
    write_json(ORDERS_DIR / str(order["id"]) / "order.json", order)
    await message.answer(
        f"Чек по заявке №{order['id']} получен и отправлен администраторам на проверку."
    )
    review_text = (
        f"<b>Чек на проверку — заявка №{order['id']}</b>\n"
        f"Пользователь: <code>{order['user_id']}</code>\n"
        f"Сумма: <b>{money(order.get('total_rub', 0))}</b>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.copy_message(
                chat_id=admin_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            await message.bot.send_message(
                admin_id,
                review_text,
                reply_markup=payment_review_keyboard(str(order["id"])),
            )
        except Exception:
            log.warning("Could not send receipt review to admin %s", admin_id)


@dp.message(Command("admin"))
async def command_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    await message.answer("Панель администратора", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin_orders")
async def admin_orders(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    orders = [load_order(path.name) for path in order_files()]
    orders = [order for order in orders if order]
    if not orders:
        await callback.message.answer("Заказов нет.")
        await callback.answer()
        return
    for order in reversed(orders):
        text = (
            f"<b>Заказ №{order['id']}</b>\n"
            f"Пользователь: <code>{order.get('user_id')}</code>\n"
            f"Сумма: {money(order.get('total_rub', 0))}\n"
            f"Статус: {html.escape(canonical_order_status(order.get('status')))}\n"
            f"Оплата: {html.escape(PAYMENT_STATUS_LABELS.get(order.get('payment_status', 'approved'), order.get('payment_status', '')))}\n"
            + "\n".join(f"• {html.escape(item['name'])}" for item in order.get("items", []))
        )
        await callback.message.answer(
            text,
            reply_markup=admin_order_keyboard(order),
        )
    await callback.answer()


@dp.callback_query(F.data == "admin_payments")
async def admin_payments(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    pending = []
    for path in order_files():
        order = load_order(path.name)
        if order and order.get("payment_status") == "receipt_uploaded":
            pending.append(order)
    if not pending:
        await callback.message.answer("Новых чеков на проверку нет.")
        await callback.answer()
        return
    for order in reversed(pending):
        await callback.message.answer(
            f"<b>Заявка №{order['id']}</b>\n"
            f"Пользователь: <code>{order.get('user_id')}</code>\n"
            f"Сумма: {money(order.get('total_rub', 0))}",
            reply_markup=payment_review_keyboard(str(order["id"])),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("payment:"))
async def payment_decision(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, decision, order_id = callback.data.split(":", 2)
    order = load_order(order_id)
    if not order:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if order.get("payment_status") != "receipt_uploaded":
        await callback.answer("Эта заявка уже обработана", show_alert=True)
        return
    approved = decision == "approve"
    order["payment_status"] = "approved" if approved else "rejected"
    order["updated_at"] = now_iso()
    write_json(ORDERS_DIR / order_id / "order.json", order)
    await callback.message.edit_reply_markup(reply_markup=None)
    if approved:
        user_message = (
            f"Оплата по заявке №{order_id} подтверждена.\n"
            "Следите за исполнением заказа в разделе /orders."
        )
        await callback.answer("Оплата подтверждена")
    else:
        user_message = (
            "Ваш заказ отказан, если это ошибка обратитесь в поддержку магазина"
        )
        await callback.answer("Оплата отклонена")
    try:
        await callback.bot.send_message(order["user_id"], user_message)
    except Exception:
        log.warning("Could not notify user %s about payment", order.get("user_id"))


@dp.callback_query(F.data.startswith("admin_stage:"))
async def admin_stage(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = callback.data.split(":", 1)[1]
    order = load_order(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    frozen = order.get("status") == ORDER_FROZEN_STATUS
    await callback.message.answer(
        (
            "Заказ заморожен. После изменения этап начнётся с «Оформлено»."
            if frozen
            else "Выберите новый этап заказа:"
        ),
        reply_markup=admin_stage_keyboard(order_id, frozen=frozen),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_stage_set:"))
async def admin_stage_set(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, order_id, index = callback.data.split(":")
    if not index.isdigit() or int(index) >= len(ORDER_STATUSES):
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    order = load_order(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    # A frozen order always resumes from the first stage.
    selected_index = 0 if order.get("status") == ORDER_FROZEN_STATUS else int(index)
    order["status"] = ORDER_STATUSES[selected_index]
    order["updated_at"] = now_iso()
    write_json(ORDERS_DIR / order_id / "order.json", order)
    await callback.answer(f"Статус: {order['status']}")


@dp.callback_query(F.data.startswith("admin_rollback:"))
async def admin_rollback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = callback.data.split(":", 1)[1]
    order = load_order(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order.get("status") == ORDER_FROZEN_STATUS:
        await callback.answer(
            "Замороженный заказ сначала нужно изменить — он начнётся с «Оформлено».",
            show_alert=True,
        )
        return
    try:
        current_index = ORDER_STATUSES.index(
            canonical_order_status(order.get("status"))
        )
    except ValueError:
        current_index = 0
    previous_index = max(0, current_index - 1)
    order["status"] = ORDER_STATUSES[previous_index]
    order["updated_at"] = now_iso()
    write_json(ORDERS_DIR / order_id / "order.json", order)
    await callback.answer(f"Статус: {order['status']}")


@dp.callback_query(F.data.startswith("admin_freeze:"))
async def admin_freeze(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = callback.data.split(":", 1)[1]
    order = load_order(order_id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order.get("status") == ORDER_FROZEN_STATUS:
        order["status"] = ORDER_STATUSES[0]
        answer = "Заказ разморожен и возвращён на этап «Оформлено»"
    else:
        order["status"] = ORDER_FROZEN_STATUS
        answer = "Заказ заморожен"
    order["updated_at"] = now_iso()
    write_json(ORDERS_DIR / order_id / "order.json", order)
    await callback.message.edit_reply_markup(reply_markup=admin_order_keyboard(order))
    await callback.answer(answer)


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    orders = [load_order(path.name) for path in order_files()]
    orders = [order for order in orders if order]
    paid_orders = [
        order
        for order in orders
        if order.get("payment_status", "approved") == "approved"
    ]
    revenue = sum(float(order.get("total_rub", 0)) for order in paid_orders)
    cost = sum(
        float(item.get("price_rub", 0)) + float(item.get("delivery_fee_rub", 0))
        for order in paid_orders
        for item in order.get("items", [])
    )
    profit = revenue - cost
    users = list(USERS_DIR.glob("*.json"))
    await callback.message.answer(
        "<b>Статистика</b>\n"
        f"Пользователей: {len(users)}\n"
        f"Заказов: {len(orders)}\n"
        f"Оплачено: {len(paid_orders)}\n"
        f"Выручка: {money(revenue)}\n"
        f"Расходы: {money(cost)}\n"
        f"Прибыль с учётом расходов: <b>{money(profit)}</b>"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_delivery")
async def admin_delivery(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.delivery)
    await callback.message.answer(
        f"Текущая доставка: {money(settings().get('delivery_fee_rub', 0))}.\n"
        "На какую сумму увеличить доставку? Отправьте прибавку в рублях:"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_payment_settings")
async def admin_payment_settings(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    current = settings()
    await state.set_state(AdminStates.payment_bank)
    await callback.message.answer(
        "Введите название банка для оплаты.\n"
        f"Текущее значение: {current.get('payment_bank_name') or 'не задано'}"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_toggle")
async def admin_toggle(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    current = settings()
    current["store_open"] = not bool(current.get("store_open", True))
    write_json(SETTINGS_PATH, current)
    await callback.message.edit_reply_markup(reply_markup=admin_keyboard())
    await callback.answer("Магазин открыт" if current["store_open"] else "Магазин закрыт")


@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    files = sorted(USERS_DIR.glob("*.json"))
    if not files:
        await callback.message.answer("Пользователей нет.")
        await callback.answer()
        return
    await callback.message.answer(f"Пользователей: {len(files)}")
    for path in files:
        user = read_json(path, {})
        if not isinstance(user, dict):
            continue
        full_name = " ".join(
            str(value).strip()
            for value in (user.get("first_name", ""), user.get("last_name", ""))
            if str(value).strip()
        ) or "Без имени"
        blocked = bool(user.get("blocked", False))
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Разблокировать" if blocked else "Заблокировать",
                        callback_data=f"admin_block:{user.get('id', path.stem)}",
                    )
                ]
            ]
        )
        await callback.message.answer(
            f"{html.escape(full_name)}\n"
            f"ID: <code>{user.get('id', path.stem)}</code>\n"
            f"@{html.escape(user.get('username', '') or 'нет')}\n"
            f"Статус: {'заблокирован' if blocked else 'активен'}",
            reply_markup=keyboard,
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_block:"))
async def admin_block(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    user_id = callback.data.split(":", 1)[1]
    path = USERS_DIR / f"{user_id}.json"
    user = read_json(path, {})
    if not isinstance(user, dict):
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    user["blocked"] = not bool(user.get("blocked", False))
    save_user(user)
    await callback.answer("Статус пользователя изменён")


@dp.callback_query(F.data == "admin_delete")
async def admin_delete(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.delete_order)
    await callback.message.answer("Отправьте номер заказа, который нужно удалить:")
    await callback.answer()


@dp.callback_query(F.data == "admin_export")
async def admin_export(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in USERS_DIR.glob("*.json"):
            archive.write(path, arcname=f"users/{path.name}")
    await callback.bot.send_document(
        callback.from_user.id,
        BufferedInputFile(stream.getvalue(), filename="users.zip"),
        caption="Архив пользователей из ./data/users",
    )
    await callback.answer("Архив отправлен")


@dp.callback_query(F.data == "admin_import")
async def admin_import(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.import_users)
    await callback.message.answer(
        "Отправьте ZIP-архив пользователей. Будут импортированы только JSON-файлы "
        "с именами пользователей, без выхода за пределы ./data/users."
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_refresh")
async def admin_refresh(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer("Переустановка позиций запущена")
    await callback.message.answer(
        "Удаляю старые позиции и заново загружаю каталог Supreme. "
        "Это может занять несколько минут…"
    )
    try:
        count = await sync_products_with_session()
        await callback.message.answer(f"Позиции переустановлены: {count} шт.")
    except Exception:
        log.exception("Manual product sync failed")
        await callback.message.answer(
            "Не удалось переустановить каталог. Старые позиции сохранены."
        )


@dp.message(AdminStates.delivery)
async def receive_delivery(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    value = parse_price(message.text or "")
    if value is None or value <= 0:
        await message.answer("Введите положительную сумму, например: 3500")
        return
    current = settings()
    old_delivery = float(current.get("delivery_fee_rub", 0))
    new_delivery = old_delivery + value
    current["delivery_fee_rub"] = round(new_delivery)
    write_json(SETTINGS_PATH, current)
    changed = reprice_catalog(new_delivery)
    await state.clear()
    await message.answer(
        f"Доставка увеличена на {money(value)}.\n"
        f"Новая стоимость доставки: {money(new_delivery)}.\n"
        f"Пересчитано позиций в каталоге: {changed}. "
        "Уже оформленные заказы не изменены."
    )


@dp.message(AdminStates.payment_bank)
async def receive_payment_bank(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    bank_name = (message.text or "").strip()
    if not bank_name:
        await message.answer("Название банка не может быть пустым.")
        return
    current = settings()
    current["payment_bank_name"] = bank_name
    write_json(SETTINGS_PATH, current)
    await state.set_state(AdminStates.payment_card)
    await message.answer("Введите номер карты, на которую переводить оплату:")


@dp.message(AdminStates.payment_card)
async def receive_payment_card(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    card_number = re.sub(r"\s+", " ", (message.text or "").strip())
    digits = re.sub(r"\D", "", card_number)
    if len(digits) < 12:
        await message.answer("Проверьте номер карты и отправьте его ещё раз.")
        return
    current = settings()
    current["payment_card_number"] = card_number
    write_json(SETTINGS_PATH, current)
    await state.set_state(AdminStates.payment_recipient)
    await message.answer(
        "Введите имя получателя или отправьте «-», если его не нужно показывать:"
    )


@dp.message(AdminStates.payment_recipient)
async def receive_payment_recipient(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    recipient = (message.text or "").strip()
    current = settings()
    current["payment_recipient"] = "" if recipient == "-" else recipient
    write_json(SETTINGS_PATH, current)
    await state.clear()
    await message.answer(
        "Реквизиты сохранены. При следующем оформлении заказа покупатель "
        "получит банк, карту и сумму перевода."
    )


@dp.message(AdminStates.delete_order)
async def receive_delete_order(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    order_id = (message.text or "").strip()
    if not order_id.isdigit():
        await message.answer("Нужен числовой номер заказа.")
        return
    directory = ORDERS_DIR / order_id
    if not directory.exists():
        await message.answer("Заказ не найден.")
        await state.clear()
        return
    shutil.rmtree(directory)
    await state.clear()
    await message.answer(f"Заказ №{order_id} удалён.")


@dp.message(AdminStates.import_users, F.document)
async def receive_import(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    document = message.document
    if not document.file_name.lower().endswith(".zip"):
        await message.answer("Нужен ZIP-файл.")
        return
    file_info = await message.bot.get_file(document.file_id)
    buffer = io.BytesIO()
    await message.bot.download_file(file_info.file_path, buffer)
    imported = 0
    try:
        with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
            for info in archive.infolist():
                name = Path(info.filename).name
                if not name or not name.endswith(".json") or name.startswith("."):
                    continue
                try:
                    payload = json.loads(archive.read(info).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
                    continue
                if not isinstance(payload, dict) or not str(payload.get("id", "")).isdigit():
                    continue
                write_json(USERS_DIR / f"{payload['id']}.json", payload)
                imported += 1
    except zipfile.BadZipFile:
        await message.answer("Архив повреждён или имеет неверный формат.")
        return
    await state.clear()
    await message.answer(f"Импортировано пользователей: {imported}.")


@dp.message(AdminStates.import_users)
async def receive_import_wrong_type(message: Message) -> None:
    await message.answer("Ожидаю ZIP-файл документом.")


@dp.message(F.document)
async def receive_receipt_document(message: Message) -> None:
    document = message.document
    await process_receipt(
        message,
        document.file_id,
        "document",
        document.file_name or "",
    )


@dp.message(F.photo)
async def receive_receipt_photo(message: Message) -> None:
    await process_receipt(message, message.photo[-1].file_id, "photo")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Run start.sh first.")
    ensure_dirs()
    write_json(SETTINGS_PATH, settings())
    log.info("Ensured ./data/users, ./data/orders and ./data/products")
    try:
        count = await sync_products_with_session()
        log.info("Initial Supreme sync: %s products", count)
    except Exception:
        log.exception("Initial catalog sync failed; bot will start with existing files")
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    log.info("Bot started. Admin IDs: %s", sorted(ADMIN_IDS))
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")