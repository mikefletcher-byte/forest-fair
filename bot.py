import asyncio
import io
import json
import logging
import os
import random
import re
import sys
import time
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
import aiohttp
from aiohttp import web

from auth_store import AuthStore, InactiveUser
from auth_api import get_bearer_session
from telegram_auth import TelegramInitDataError, verify_telegram_init_data


# =====================================================================
# КОНФИГУРАЦИЯ И КЛЮЧИ
# =====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_secret(filename: str, fallback: str = "") -> str:
    path = os.path.join(BASE_DIR, filename) if not os.path.isabs(filename) else filename
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return fallback
    return fallback


TELEGRAM_TOKEN = load_secret("tg_token.txt", "")
GEMINI_API_KEY = load_secret("key.txt", "")
VK_TOKEN = load_secret("vk_token.txt", "")
VK_GROUP_ID = 236995482
VK_ALBUM_ID = 312072854

# Пути для поиска картинок блога для Telegram
CANDIDATE_PICS_DIRS = [
    "/root/vkpict",
    "/root/forest_bot/vkpict",
    os.path.join(BASE_DIR, "vkpict"),
    r"E:\forest_bot\vkpict",
    os.path.join(BASE_DIR, "Pictures"),
]

BLOG_INDEX_FILE = os.path.join(BASE_DIR, "blog_pic_index.txt")

WEBAPP_URL = "https://mikefletcher-byte.github.io/forest-fair/"
TG_SHOP_LINK = "https://t.me/ForestFairTomsk_bot?start=shop"
MANAGER_USERNAME = "MBarukh"
GROUP_USERNAME = "@ForestFair70"

log_handlers = [logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8")]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=log_handlers,
)

CACHED_LOGO_ID = None
CHAT_HISTORIES = {}
VK_CHAT_HISTORIES = {}
MANAGER_CHAT_ID_FILE = os.path.join(BASE_DIR, "manager_id.txt")
ADMIN_CHAT_ID_FILE = os.path.join(BASE_DIR, "admin_id.txt")
REMINDERS_FILE = os.path.join(BASE_DIR, "reminders.json")
ORDERS_FILE = os.path.join(BASE_DIR, "orders.json")
AUTH_DB_FILE = os.environ.get("FORESTFAIR_AUTH_DB", os.path.join(BASE_DIR, "auth.sqlite3"))

FILE_LOCK = asyncio.Lock()


def get_saved_id(filepath: str):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return int(f.read().strip())
        except Exception:
            return None
    return None


async def save_id_async(filepath: str, chat_id: int):
    async with FILE_LOCK:
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(str(chat_id))
        except Exception as e:
            logging.warning("Не удалось сохранить ID в %s: %s", filepath, e)


async def check_and_save_manager(user):
    if not user:
        return
    if user.username and user.username.lower() == MANAGER_USERNAME.lower():
        await save_id_async(MANAGER_CHAT_ID_FILE, user.id)
    elif get_saved_id(ADMIN_CHAT_ID_FILE) is None:
        await save_id_async(ADMIN_CHAT_ID_FILE, user.id)


def load_reminders():
    if os.path.exists(REMINDERS_FILE):
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


async def save_reminders_async(data):
    async with FILE_LOCK:
        try:
            with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning("Ошибка сохранения reminders.json: %s", e)


async def register_customer_order(user_id: int, full_name: str):
    async with FILE_LOCK:
        reminders = load_reminders()
        now_ts = time.time()
        reminders.append(
            {
                "user_id": user_id,
                "name": full_name,
                "order_ts": now_ts,
                "review_sent": False,
                "reactivate_sent": False,
            }
        )
        try:
            with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(reminders, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning("Ошибка сохранения reminders.json: %s", e)


# =====================================================================
# БАЗА ЗАКАЗОВ И СТАТУСОВ
# =====================================================================
def load_orders() -> dict:
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


async def save_orders_async(data: dict):
    async with FILE_LOCK:
        try:
            with open(ORDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning("Ошибка сохранения orders.json: %s", e)


async def create_saved_order(data: dict, tg_user_id=None) -> dict:
    async with FILE_LOCK:
        orders = load_orders()
        next_num = 101 + len(orders)
        order_id = str(next_num)
        while order_id in orders:
            next_num += 1
            order_id = str(next_num)

        now_str = time.strftime("%d.%m.%Y %H:%M", time.localtime())
        record = {
            "order_id": order_id,
            "created_at": now_str,
            "customer_name": data.get("customer_name", "Покупатель"),
            "phone": data.get("phone", "Не указан"),
            "address": data.get("address", "Не указан"),
            "delivery_method": data.get("delivery_method", "Курьер / Доставка"),
            "payment_method": data.get("payment_method", "Переводом"),
            "items": data.get("items", []),
            "total": data.get("total", 0),
            "status_code": "new",
            "status_text": "🟡 Принят, ожидает подтверждения менеджера",
            "track_number": "",
            "tg_user_id": tg_user_id,
        }
        orders[order_id] = record
        try:
            with open(ORDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(orders, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning("Ошибка сохранения orders.json: %s", e)
        return record


def get_logo_image():
    for filename in ["logo.jpg", "logo.jpeg", "logo.png"]:
        p = os.path.join(BASE_DIR, filename)
        if os.path.exists(p):
            return FSInputFile(p)
    return None


def get_review_banner_image():
    paths = [
        os.path.join(BASE_DIR, "Pictures", "review_banner.jpg"),
        os.path.join(BASE_DIR, "Pictures", "review_banner.jpeg"),
        os.path.join(BASE_DIR, "Pictures", "review_banner.png"),
        os.path.join(BASE_DIR, "review_banner.jpg"),
        os.path.join(BASE_DIR, "logo.jpg"),
    ]
    for p in paths:
        if os.path.exists(p):
            return FSInputFile(p)
    return None


def clean_html_for_vk(text: str) -> str:
    """
    Преобразует HTML-разметку в формат ВКонтакте:
    1. Для внутренних ссылок ВК делает кликабельный синий текст [clubID|Текст].
    2. Для ссылки на бота подменяет её на витрину магазина.
    3. Для внешних ссылок формирует запись без круглых скобок.
    4. Счищает остальные HTML-теги.
    """
    def replace_link(match):
        href = match.group(1).strip()
        label = match.group(2).strip()

        # 1. Если попала ссылка на бота (t.me/...start=shop) — заменяем на веб-витрину
        if "t.me" in href and "start=shop" in href:
            href = WEBAPP_URL

        # 2. Если ссылка ведёт на саму группу ВК
        if f"club{VK_GROUP_ID}" in href or f"public{VK_GROUP_ID}" in href or "forestfair70" in href:
            return f"[club{VK_GROUP_ID}|{label}]"

        # 3. Если ссылка на другое сообщество ВК
        vk_club_match = re.search(r"vk\.com/(?:club|public)(\d+)", href)
        if vk_club_match:
            return f"[club{vk_club_match.group(1)}|{label}]"

        # 4. Внешняя ссылка без круглых скобок ()
        display_url = re.sub(r"^https?://", "", href).rstrip("/")
        if label and label != href:
            return f"{label}: {display_url}"
        return display_url

    text = re.sub(
        r'<a\s+[^>]*href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>',
        replace_link,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"</?[a-zA-Z0-9]+[^>]*>", "", text)
    return text.strip()


# =====================================================================
# НАСТРОЙКА GEMINI REST API
# =====================================================================
SYSTEM_PROMPT = """
Ты — вежливый, теплый и экспертный онлайн-консультант магазина таежной продукции и дикоросов «Forest Fair — Лесная Ярмарка» (г. Томск).
Общайся в мужском роде (например: «Я рад помочь», «Подскажу»). Никогда не называй себя искусственным интеллектом, нейросетью или ботом — ты живой эксперт-консультант таежной лавки.

ВАЖНЕЙШИЕ ПРАВИЛА ДИАЛОГА:
1. НЕ ЗДОРОВАЙСЯ ЗАНОВО в каждом сообщении! Если в истории диалога ты уже поздоровался с клиентом, сразу переходи к сути ответа.
2. НЕ ПОВТОРЯЙ в каждом сообщении одни и те же шаблонные абзацы про телефон менеджера и промокод FOREST10, если уже упоминал их ранее.
3. НЕ ИСПОЛЬЗУЙ символы звездочек ** для жирного шрифта. Для списков используй буллиты • или подходящие эмодзи 🌲🍄🍵🌿.
4. В живой речи вместо прямого слова «мухомор» используй точные названия из нашего ассортимента: «Красный колпак», «Пантерный колпак», «Пантерные шляпки» (обрати внимание: красных раскрытых шляпок у нас нет, красный идет только в форме отборных колпаков 1-го и 2-го сорта!).

АКТУАЛЬНЫЙ КАТАЛОГ И ЦЕНЫ:
Грибы и дикоросы:
• Красный (колпак 1-й сорт) — 1 250 ₽ (50 г)
• Красный (колпак 2-й сорт) — 1 000 ₽ (50 г)
• Пантерный (колпак 1-й сорт) — 4 000 ₽ (50 г)
• Пантерный (колпак 2-й сорт) — 3 750 ₽ (50 г)
• Пантерный (шляпки 1-й сорт) — 3 500 ₽ (50 г)
• Пантерный (шляпки 2-й сорт) — 3 000 ₽ (50 г)
• Лисичка свежая (1-й сорт) — 4 200 ₽ (1 кг)
• Лисичка свежая (2-й сорт) — 3 800 ₽ (1 кг)

Ежовик и таежные сборы:
• Мицелий Ежовика Гребенчатого (Россия) — 500 ₽ (100 г)
• Мицелий Ежовика Гребенчатого Опт (Россия) — 25 000 ₽ (10 кг)
• Таежный сбор трав (антипаразитарный) — 650 ₽ (50 г)

Премиальные чаи:
• Габба Рубиновая — 1 000 ₽ (50 г)
• Габба Топаз — 1 000 ₽ (50 г)
• Габба Бриллиант — 1 000 ₽ (50 г)
• Пуэр Королевский — 600 ₽ (50 г)
• Пуэр Ся-То — 600 ₽ (50 г)

УСЛОВИЯ ДОСТАВКИ И АКЦИИ:
- Витрина магазина с выбором веса: оформляй ссылку СТРОГО через HTML: <a href='https://mikefletcher-byte.github.io/forest-fair/'>Онлайн-витрина «Forest Fair»</a>
- Способы получения: Курьером / Доставка (500 ₽, а при заказе от 5 000 ₽ — БЕСПЛАТНО!) или Самовывоз в г. Томск.
- Оплата: Переводом или Наличными.
- Промокод FOREST10 дает скидку 10% на любую продукцию!
- Менеджер: Матвей (@MBarukh), телефон: +7(996)-636-45-66.
- Строго 18+. Продукция не является лекарственным средством.
"""

MODELS_POOL = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-flash-lite-latest",
    "gemini-1.5-flash",
]


async def update_models_pool():
    global MODELS_POOL
    if not GEMINI_API_KEY:
        return
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    headers = {"X-goog-api-key": GEMINI_API_KEY}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    available = []
                    for m in data.get("models", []):
                        name = m.get("name", "").replace("models/", "")
                        if "flash" in name:
                            available.append(name)
                    if available:
                        available.sort(key=lambda x: 0 if "2.5" in x or "2.0" in x else 1)
                        MODELS_POOL = available
                        logging.info("Актуальный пул моделей Gemini: %s", MODELS_POOL[:5])
        except Exception as e:
            logging.warning("Не удалось обновить пул моделей: %s", e)


async def call_gemini_rest(contents_list, use_system=True):
    headers = {"Content-Type": "application/json", "X-goog-api-key": GEMINI_API_KEY}
    payload = {"contents": contents_list, "generationConfig": {"temperature": 0.7}}
    if use_system:
        payload["systemInstruction"] = {"parts": [{"text": SYSTEM_PROMPT}]}

    async with aiohttp.ClientSession() as session:
        for model_name in MODELS_POOL:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + model_name
                + ":generateContent"
            )
            try:
                async with session.post(
                    url, headers=headers, json=payload, timeout=25
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        return text.replace("**", "")
                    else:
                        err_body = await resp.text()
                        logging.warning("Модель %s вернула %s: %s", model_name, resp.status, err_body)
            except Exception as e:
                logging.warning("Ошибка соединения с %s: %s", model_name, e)
            await asyncio.sleep(0.4)
    return None


# =====================================================================
# СИНХРОННЫЙ ВЫБОР ОБЛОЖКИ ДЛЯ TG И VK ПО ПОРЯДКУ
# =====================================================================
def get_and_advance_blog_index() -> int:
    idx = 0
    if os.path.exists(BLOG_INDEX_FILE):
        try:
            with open(BLOG_INDEX_FILE, "r", encoding="utf-8") as f:
                idx = int(f.read().strip())
        except Exception:
            idx = 0

    next_idx = idx + 1
    try:
        with open(BLOG_INDEX_FILE, "w", encoding="utf-8") as f:
            f.write(str(next_idx))
    except Exception as e:
        logging.warning("Не удалось сохранить индекс блога: %s", e)

    return idx


def get_tg_blog_image_by_index(idx: int) -> str | None:
    target_dir = None
    for p in CANDIDATE_PICS_DIRS:
        if os.path.exists(p) and os.path.isdir(p):
            target_dir = p
            break

    if not target_dir:
        return None

    valid_exts = (".jpg", ".jpeg", ".png", ".webp")
    files = sorted(
        [
            os.path.join(target_dir, f)
            for f in os.listdir(target_dir)
            if f.lower().endswith(valid_exts) and "logo" not in f.lower()
        ]
    )

    if not files:
        return None

    return files[idx % len(files)]


async def get_vk_album_covers(session: aiohttp.ClientSession) -> list[str]:
    """Считывает список фото из альбома ВК в оригинальном порядке"""
    url_desktop = f"https://vk.com/album-{VK_GROUP_ID}_{VK_ALBUM_ID}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        async with session.get(url_desktop, headers=headers, ssl=False, timeout=12) as resp:
            if resp.status == 200:
                html = await resp.text()
                raw_ids = re.findall(r"photo-" + str(VK_GROUP_ID) + r"_(\d+)", html)
                if not raw_ids:
                    raw_ids = re.findall(r'"-' + str(VK_GROUP_ID) + r'_(\d+)"', html)
                
                seen = set()
                ordered = []
                for pid in raw_ids:
                    if pid not in seen:
                        seen.add(pid)
                        ordered.append(f"photo-{VK_GROUP_ID}_{pid}")
                if ordered:
                    return ordered
    except Exception as e:
        logging.warning("Не удалось спарсить альбом ВК: %s", e)
    return ["photo-236995482_457239083"]


# =====================================================================
# ВКОНТАКТЕ API И ПУБЛИКАЦИЯ НА СТЕНУ
# =====================================================================
async def vk_call_api(
    session: aiohttp.ClientSession, method: str, params: dict
) -> dict:
    if not VK_TOKEN:
        return {}
    params["access_token"] = VK_TOKEN
    params["v"] = "5.199"
    url = "https://api.vk.com/method/" + method
    try:
        async with session.post(url, data=params, timeout=20) as resp:
            data = await resp.json()
            if "error" in data:
                logging.warning("VK API Error (%s): %s", method, data["error"])
            return data
    except Exception as e:
        logging.warning("Ошибка соединения с VK API (%s): %s", method, e)
        return {}


async def publish_vk_wall_post(
    session: aiohttp.ClientSession, post_text: str, current_idx: int = 0
):
    if not VK_TOKEN:
        logging.warning("VK_TOKEN не найден, публикация в ВК пропущена.")
        return

    clean_text = clean_html_for_vk(post_text)

    # Берем фото из альбома под тем же порядковым номером, что и в Telegram
    covers = await get_vk_album_covers(session)
    chosen_cover = covers[current_idx % len(covers)] if covers else "photo-236995482_457239083"

    params = {
        "owner_id": "-" + str(VK_GROUP_ID),
        "from_group": "1",
        "message": clean_text,
        "attachments": chosen_cover,
    }
    res = await vk_call_api(session, "wall.post", params)
    if res.get("response", {}).get("post_id"):
        logging.info(
            "Пост блога успешно опубликован в ВК! ID: %s (Обложка №%s: %s)",
            res["response"]["post_id"],
            (current_idx % len(covers)) + 1 if covers else 1,
            chosen_cover,
        )
    else:
        logging.error("Ошибка при публикации поста в ВК: %s", res)


# =====================================================================
# КЛАВИАТУРЫ И ШАБЛОНЫ TELEGRAM
# =====================================================================
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="🛒 Открыть Магазин", web_app=WebAppInfo(url=WEBAPP_URL)
            )
        ],
        [
            KeyboardButton(text="🍄 Каталог и цены"),
            KeyboardButton(text="📖 Памятка по приему и завариванию"),
        ],
        [
            KeyboardButton(text="📦 Оформить заказ"),
            KeyboardButton(text="🎁 Скидка / Промокод"),
        ],
    ],
    resize_keyboard=True,
    input_field_placeholder="Откройте магазин или задайте вопрос...",
)


def get_manager_order_kb(
    order_id: str, tg_user_id=None, full_name="Покупатель"
) -> InlineKeyboardMarkup:
    rows = []
    if tg_user_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💬 Написать покупателю (" + str(full_name) + ")",
                    url="tg://user?id=" + str(tg_user_id),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔵 Собирается", callback_data="st_" + str(order_id) + "_pack"
            ),
            InlineKeyboardButton(
                text="🚚 Отправлен", callback_data="st_" + str(order_id) + "_ship"
            ),
            InlineKeyboardButton(
                text="🟢 Выполнен", callback_data="st_" + str(order_id) + "_done"
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_group_post_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Открыть Магазин",
                    url="https://t.me/ForestFairTomsk_bot?start=shop"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Консультация и Подбор продукции",
                    url="https://t.me/ForestFairTomsk_bot?start=welcome"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🍄 Посмотреть Каталог и цены",
                    url="https://t.me/ForestFairTomsk_bot?start=catalog"
                )
            ]
        ]
    )


def get_blog_post_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Открыть Магазин",
                    url="https://t.me/ForestFairTomsk_bot?start=shop"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Обратная связь / Консультация",
                    url="https://t.me/ForestFairTomsk_bot?start=welcome"
                )
            ]
        ]
    )


def get_review_topic_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Открыть Магазин",
                    url="https://t.me/ForestFairTomsk_bot?start=shop",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Написать менеджеру Матвею",
                    url="https://t.me/MBarukh",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✍️ Отзывы",
                    url="https://t.me/ForestFair70/290",
                )
            ],
        ]
    )


WELCOME_TEXT = (
    "🌲 <b>Добро пожаловать в «Forest Fair — Лесная Ярмарка»!</b> 🌲\n\n"
    "Мы находимся в сердце Сибири — в г. <b>Томск</b>, и бережно заготавливаем для вас "
    "таежную продукцию, целебные дикоросы, лесные грибы и премиальные чаи.\n\n"
    "🛒 <b>Нажмите кнопку «Открыть Магазин» внизу экрана</b>, чтобы открыть интерактивную витрину с расчетом веса и оформлением доставки!\n\n"
    "🌿 Или <b>просто напишите любой вопрос прямо в этот чат</b> — мы подробно проконсультируем вас!\n\n"
    "🔗 <b>Наши контакты:</b>\n"
    "• Сообщество TG: <a href='https://t.me/ForestFair70'>Forest Fair — Лесная Ярмарка</a>\n"
    "• Сообщество VK: <a href='https://vk.com/forestfair70'>vk.com/forestfair70</a>\n"
    "• Заказ у менеджера: @MBarukh (Матвей)\n"
    "• 💻 Digital-архитектура: @DJonWeaK"
)

GROUP_POST_TEXT = (
    "🌲 <b>«Forest Fair — Лесная Ярмарка» (г. Томск)</b> 🌲\n\n"
    "Добро пожаловать в нашу таежную лавку! Мы бережно собираем и заготавливаем "
    "сибирские дикоросы, лесные грибы, мицелий ежовика, травяные сборы и премиальные чаи (Габба, Пуэр).\n\n"
    "🎁 Промокод <b>FOREST10</b> дает скидку <b>10%</b> на любую продукцию!\n"
    "🚚 Бесплатная доставка при заказе от 5 000 ₽!\n\n"
    "👤 <b>Заказы и консультации:</b> @MBarukh (Матвей)\n"
    "💻 <b>Автор Digital-архитектуры:</b> @DJonWeaK\n\n"
    "👇 <b>Нажмите на нужную кнопку ниже для перехода в витрину или к консультанту:</b>"
)

CATALOG_TEXT = (
    "🍄 <b>АКТУАЛЬНЫЙ КАТАЛОГ И ЦЕНЫ «Forest Fair»</b> 🌲\n\n"
    "🔴 <b>Красный (колпак 1-й сорт)</b> — <b>1 250 ₽</b> (50 г)\n"
    "🔴 <b>Красный (колпак 2-й сорт)</b> — <b>1 000 ₽</b> (50 г)\n"
    "✨ <i>Глубокий сон, снятие тревожности и стресса, мягкий вечерний профилактический курс.</i>\n\n"
    "🐆 <b>Пантерный (колпак 1-й сорт)</b> — <b>4 000 ₽</b> (50 г)\n"
    "🐆 <b>Пантерный (колпак 2-й сорт)</b> — <b>3 750 ₽</b> (50 г)\n"
    "🐆 <b>Пантерный (шляпки 1-й сорт)</b> — <b>3 500 ₽</b> (50 г)\n"
    "🐆 <b>Пантерный (шляпки 2-й сорт)</b> — <b>3 000 ₽</b> (50 г)\n"
    "✨ <i>Высокая природная концентрация и активность для опытных ценителей.</i>\n\n"
    "🧡 <b>Лисичка свежая (1-й сорт)</b> — <b>4 200 ₽</b> (1 кг)\n"
    "🧡 <b>Лисичка свежая (2-й сорт)</b> — <b>3 800 ₽</b> (1 кг)\n"
    "✨ <i>Отборная свежая сибирская лисичка, природное очищение и защита организма.</i>\n\n"
    "🧠 <b>Мицелий Ежовика (Россия)</b> — <b>500 ₽</b> (от 100 г)\n"
    "🧠 <b>Мицелий Ежовика Опт (Россия)</b> — <b>25 000 ₽</b> (от 10 кг)\n"
    "✨ <i>Российский природный ноотроп для памяти, концентрации внимания и нейрогенеза.</i>\n\n"
    "🍵 <b>Премиальный чай Габба:</b>\n"
    "• <b>Габба Рубиновая</b> — <b>1 000 ₽</b> (50 г)\n"
    "• <b>Габба Топаз</b> — <b>1 000 ₽</b> (50 г)\n"
    "• <b>Габба Бриллиант</b> — <b>1 000 ₽</b> (50 г)\n"
    "✨ <i>Высокое содержание природной ГАМК, снятие напряжения без сонливости, ясность мыслей.</i>\n\n"
    "☕️ <b>Выдержанный чай Пуэр:</b>\n"
    "• <b>Пуэр Королевский</b> — <b>600 ₽</b> (50 г)\n"
    "• <b>Пуэр Ся-То</b> — <b>600 ₽</b> (50 г)\n"
    "✨ <i>Мощная ровная бодрость на весь день, здоровая альтернатива кофе и тонус ЖКТ.</i>\n\n"
    "🌿 <b>Таежный сбор трав (Антипаразитарный)</b> — <b>650 ₽</b> (50 г)\n\n"
    "➖➖➖➖➖➖➖➖➖➖➖➖\n"
    "🚚 <b>Доставка:</b> 500 ₽ (при заказе от 5 000 ₽ — <b>БЕСПЛАТНО!</b>) или самовывоз в г. Томск.\n"
    "🎁 <b>Промокод FOREST10</b> дает скидку 10% на любую продукцию!\n\n"
    "🛒 <b>Нажмите кнопку «Открыть Магазин» внизу</b>, чтобы выбрать вес и оформить заказ!"
)

GUIDE_TEXT = (
    "📖 <b>ПАМЯТКА ПО ПРИЕМУ И ЗАВАРИВАНИЮ ДАРОВ «FOREST FAIR»</b> 🌲\n\n"
    "🍄 <b>1. Красный колпак, Пантерный колпак и Пантерные шляпки:</b>\n"
    "• <b>Форма приема:</b> измельченную часть или цельный кусочек запивают теплой водой (до 40°C). Не заливайте крутым кипятком!\n"
    "• <b>Рекомендуемые микро-порции:</b>\n"
    "  — <i>Красный колпак:</i> 0.3 – 0.5 г утром натощак (для ровного фона) и/или 0.5 г вечером за 1 час до сна (для глубокого восстановления).\n"
    "  — <i>Пантерный (колпак / шляпки):</i> начинайте строго с минимальной порции 0.15 – 0.25 г (он в 3–4 раза активнее красного!).\n"
    "• <b>Хранение:</b> в плотно закрытом пакете в темном сухом месте при комнатной температуре.\n\n"
    "🧠 <b>2. Мицелий Ежовика Гребенчатого:</b>\n"
    "• <b>Форма приема:</b> по 1 чайной ложке без горки (3–5 г) 1–2 раза в день за 20 минут до еды, запивая теплой водой или добавляя в теплый напиток.\n"
    "• Отлично сочетается с профилактическим курсом и витамином С.\n\n"
    "🍵 <b>3. Чай Габба (Рубиновая / Топаз / Бриллиант):</b>\n"
    "• <b>Температура воды:</b> 85–90°C (дайте кипятку остыть 3–4 минуты).\n"
    "• <b>Пропорция:</b> 5–7 г заварки на чайник или гайвань (150–250 мл).\n"
    "• Выдерживает до 6–8 проливов, раскрывая медово-фруктовые ноты и мягкое состояние спокойного фокуса.\n\n"
    "☕️ <b>4. Чай Пуэр (Королевский / Ся-То):</b>\n"
    "• <b>Температура воды:</b> 95–98°C. Первую заварку (промывку) слейте через 5 секунд, чтобы разбудить чайный лист.\n"
    "• Пейте в первой половине дня для мощного заряда ровной энергии!\n\n"
    "🌿 <b>5. Таежный сбор трав:</b>\n"
    "• 1 столовую ложку сбора залить 250 мл горячей воды (85–90°C), настоять 20–30 минут под крышкой. Принимать по 1/3 стакана за 20 минут до еды.\n\n"
    "💚 <i>Соблюдайте умеренность, слушайте свой организм и пусть сила сибирской тайги идет вам во благо!</i>"
)

ORDER_TEXT = (
    "📦 <b>КАК ОФОРМИТЬ ЗАКАЗ</b>\n\n"
    "1️⃣ <b>Через витрину:</b> нажмите кнопку <b>«🛒 Открыть Магазин»</b> внизу, выберите нужную продукцию, укажите способ доставки (Курьер / Самовывоз) и подтвердите заказ.\n\n"
    "2️⃣ <b>Напрямую у менеджера:</b>\n"
    "👤 <b>Менеджер:</b> Матвей — @MBarukh\n"
    "📞 <b>Телефон:</b> <code>+7(996)-636-45-66</code>\n\n"
    "🚚 <b>Условия доставки:</b>\n"
    "• Доставка курьером / транспортной службой — <b>500 ₽</b>.\n"
    "• При заказе от <b>5 000 ₽</b> доставка осуществляется <b>БЕСПЛАТНО!</b>\n"
    "• Самовывоз в г. Томск.\n\n"
    "🎁 <i>Промокод <b>FOREST10</b> дает скидку 10% на любую продукцию!</i>"
)

PROMO_TEXT = (
    "🎁 <b>ВАША СКИДКА 10%</b>\n\n"
    "🎁 Промокод <b>FOREST10</b> дает скидку <b>10%</b> на любую продукцию!\n\n"
    "🔥 Код для копирования: <code>FOREST10</code>\n\n"
    "Активируйте его одной кнопкой внутри нашей витрины <b>«🛒 Открыть Магазин»</b> "
    "или назовите менеджеру @MBarukh при заказе!"
)

REVIEW_TOPIC_TEXT = (
    "🌲 <b>ОТЗЫВЫ И ВПЕЧАТЛЕНИЯ ГОСТЕЙ «FOREST FAIR»</b> 🌲\n\n"
    "Дорогие друзья, гости и ценители сибирской природы!\n\n"
    "В этой ветке вы можете открыто поделиться своими впечатлениями о продукции нашей таежной лавки:\n"
    "• О качестве сбора дикоросов, свежих лисичек и травяных сборов\n"
    "• Об эффекте и ощущениях от курсов Ежовика и целебных грибов\n"
    "• О вкусе и аромате премиальных сортов чая Габба и Пуэр\n\n"
    "💬 <i>Нам очень важно ваше честное мнение, а ваши отзывы помогают новым гостям определиться с выбором!</i>\n\n"
    "🎁 <b>За каждый оставленный отзыв мы дарим приятный таежный комплимент к вашему следующему заказу!</b> 🍵\n\n"
    "🛒 <a href='https://t.me/ForestFairTomsk_bot?start=shop'>Онлайн-витрина лавки</a>\n\n"
    "👤 <b>Заказы и связь с менеджером:</b> @MBarukh (Матвей, тел. +7(996)-636-45-66)"
)

BLOG_TOPICS = [
    "Удивительные дикоросы Сибири и Алтая: как таежные травы помогали сибирским охотникам сохранять выносливость",
    "Влияние утренней разминки, дыхательных практик и контрастного душа на нейропластичность и уровень энергии",
    "История чайной культуры: как ферментированные чаи изменили традиции общения и концентрации внимания на Востоке",
    "Циркадные ритмы и гигиена сна: почему вечерний цифровой детокс и травяной настой восстанавливают нервную систему",
    "Грибное царство в истории человечества: интересные этноботанические и научные факты о лесных грибах",
    "Как прогулки в хвойном лесу (фитонциды сосны и кедра) влияют на иммунитет и снижение уровня кортизола",
]

dp = Dispatcher()


def format_order_core_text(data: dict, order_id: str = "") -> str:
    items = data.get("items", [])
    promo = data.get("promo", "Нет")
    goods_total = data.get("goods_total", 0)
    delivery_method = data.get("delivery_method", "Курьер / Доставка")
    delivery_cost = data.get("delivery_cost", 0)
    address = data.get("address", "Не указан")
    phone = data.get("phone", "Не указан")
    payment_method = data.get("payment_method", "Переводом")
    comment = data.get("comment", "Нет")
    total = data.get("total", 0)

    header_title = (
        "🌲 <b>ЗАКАЗ #" + str(order_id) + " В «FOREST FAIR»</b> 🌲\n"
        if order_id
        else "🌲 <b>ЗАКАЗ В «FOREST FAIR»</b> 🌲\n"
    )
    core_lines = [header_title]
    core_lines.append("📦 <b>Состав корзины:</b>")

    for idx, item in enumerate(items, start=1):
        title = item.get("title", "Товар")
        grams = item.get("grams", 0)
        subtotal = item.get("subtotal", 0)
        w_label = (
            str(grams // 1000) + " кг"
            if (grams >= 1000 and grams % 1000 == 0)
            else str(grams) + " г"
        )
        core_lines.append(
            "• "
            + str(idx)
            + ". <b>"
            + str(title)
            + "</b> — "
            + w_label
            + " ("
            + str(subtotal)
            + " ₽)"
        )

    if delivery_method == "Самовывоз":
        deliv_line = "Самовывоз"
    else:
        deliv_str = "Бесплатно" if delivery_cost == 0 else str(delivery_cost) + " ₽"
        deliv_line = str(delivery_method) + " (" + deliv_str + ")"

    core_lines.append("\n🎁 <b>Промокод:</b> " + str(promo))
    core_lines.append("🛍 <b>Сумма товаров:</b> " + str(goods_total) + " ₽")
    core_lines.append("🚚 <b>Способ получения:</b> " + deliv_line)
    core_lines.append("📍 <b>Адрес:</b> " + str(address))
    core_lines.append("📞 <b>Телефон:</b> <code>" + str(phone) + "</code>")
    core_lines.append("💳 <b>Оплата:</b> " + str(payment_method))
    core_lines.append("💬 <b>Комментарий:</b> " + str(comment))
    core_lines.append("\n💰 <b>ИТОГО К ОПЛАТЕ:</b> <b>" + str(total) + " ₽</b>")

    return "\n".join(core_lines)


async def send_manager_alerts(bot: Bot, notification_text: str, reply_markup=None):
    manager_id = get_saved_id(MANAGER_CHAT_ID_FILE)
    admin_id = get_saved_id(ADMIN_CHAT_ID_FILE)

    if manager_id:
        try:
            await bot.send_message(
                chat_id=manager_id,
                text=notification_text,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logging.warning("Не удалось отправить менеджеру (%s): %s", manager_id, e)

    if admin_id and admin_id != manager_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=notification_text,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logging.warning("Не удалось отправить админу (%s): %s", admin_id, e)


async def send_photo_fast(message: Message, caption: str, reply_markup):
    global CACHED_LOGO_ID

    if CACHED_LOGO_ID:
        try:
            return await message.answer_photo(
                photo=CACHED_LOGO_ID,
                caption=caption,
                reply_markup=reply_markup,
            )
        except Exception:
            CACHED_LOGO_ID = None

    photo = get_logo_image()
    if photo:
        sent_msg = await message.answer_photo(
            photo=photo,
            caption=caption,
            reply_markup=reply_markup,
        )
        if sent_msg.photo:
            CACHED_LOGO_ID = sent_msg.photo[-1].file_id
        return sent_msg
    else:
        return await message.answer(
            text=caption,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


async def generate_blog_article() -> str:
    topic = random.choice(BLOG_TOPICS)
    prompt = (
        "Напиши увлекательный, познавательный и достоверный пост для блога нашего сообщества «Forest Fair — Лесная Ярмарка» (г. Томск) "
        "на тему: «" + topic + "».\n"
        "Требования:\n"
        "1. Заголовок и сам текст (3–4 емких абзаца, легко читаемых с телефона).\n"
        "2. Тон: теплый, научно-популярный, вдохновляющий на здоровый образ жизни и уважение к сибирской природе.\n"
        "3. Не используй символы звездочек ** (пиши чистым текстом с красивыми эмодзи 🌲🌿🍵).\n"
        "4. В конце текста С НОВОЙ СТРОКИ отдельным абзацем оформи витрину строго так:\n\n"
        f"🛒 <a href='{TG_SHOP_LINK}'>Онлайн-витрина «Forest Fair»</a>\n"
        "(не забудьте использовать промокод FOREST10 на скидку 10%!)."
    )
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    reply = await call_gemini_rest(contents, use_system=False)

    if reply:
        return "🌲 РУБРИКА «ЛЕСНОЙ БЛОГ FOREST FAIR» 🌲\n\n" + reply.strip()

    return (
        "🌲 РУБРИКА «ЛЕСНОЙ БЛОГ FOREST FAIR» 🌲\n\n"
        "🌿 Сибирская тайга хранит в себе уникальную природную силу. "
        "Бережный сбор трав и целебных дикоросов позволяет поддержать тонус и ясность ума.\n\n"
        f"🛒 <a href='{TG_SHOP_LINK}'>Онлайн-витрина «Forest Fair»</a>\n"
        "(не забудьте использовать промокод FOREST10 на скидку 10%!)."
    )


# =====================================================================
# ОБРАБОТЧИКИ TELEGRAM-БОТА
# =====================================================================
@dp.callback_query(F.data.startswith("st_"))
async def handle_status_change(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.answer("Ошибка данных")
        return

    order_id = parts[1]
    action = parts[2]

    status_map = {
        "pack": ("pack", "🔵 Оплачен / Бережно собирается в лавке"),
        "ship": ("ship", "🚚 Отправлен в доставку"),
        "done": ("done", "🟢 Выполнен / Получен клиентом"),
    }

    if action not in status_map:
        await callback.answer("Неизвестный статус")
        return

    orders = load_orders()
    if order_id not in orders:
        await callback.answer("Заказ #" + order_id + " не найден в базе", show_alert=True)
        return

    code, label = status_map[action]
    orders[order_id]["status_code"] = code
    orders[order_id]["status_text"] = label
    await save_orders_async(orders)

    tg_uid = orders[order_id].get("tg_user_id")
    if tg_uid:
        try:
            await bot.send_message(
                chat_id=tg_uid,
                text="📦 <b>Обновление статуса вашего заказа #"
                + str(order_id)
                + ":</b>\n"
                + label,
            )
        except Exception:
            pass

    await callback.answer("Статус заказа #" + order_id + " изменен: " + label, show_alert=True)


@dp.message(Command("track"))
async def cmd_set_track(message: Message, bot: Bot):
    await check_and_save_manager(message.from_user)
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Использование: <code>/track НОМЕР_ЗАКАЗА ТРЕК_НОМЕР</code>\nНапример: <code>/track 101 80085553535</code>"
        )
        return

    order_id = parts[1].replace("#", "").strip()
    track_num = parts[2].strip()

    orders = load_orders()
    if order_id not in orders:
        await message.answer("❌ Заказ #" + order_id + " не найден.")
        return

    orders[order_id]["track_number"] = track_num
    orders[order_id]["status_code"] = "ship"
    orders[order_id]["status_text"] = "🚚 Отправлен (Трек: " + track_num + ")"
    await save_orders_async(orders)

    tg_uid = orders[order_id].get("tg_user_id")
    if tg_uid:
        try:
            await bot.send_message(
                chat_id=tg_uid,
                text="🚚 <b>Ваш заказ #"
                + order_id
                + " отправлен!</b>\nТрек-номер для отслеживания: <code>"
                + track_num
                + "</code>",
            )
        except Exception:
            pass

    await message.answer(
        "✅ Трек-номер <code>"
        + track_num
        + "</code> успешно прикреплен к заказу #"
        + order_id
        + "!"
    )


@dp.message(Command("post"))
@dp.channel_post(Command("post"))
async def cmd_post_for_group(message: Message):
    await check_and_save_manager(message.from_user)
    try:
        await message.delete()
    except Exception:
        pass
    post_msg = await send_photo_fast(message, GROUP_POST_TEXT, get_group_post_kb())
    if post_msg:
        try:
            await post_msg.pin(disable_notification=False)
        except Exception as e:
            logging.warning("Не удалось закрепить сообщение: %s", e)

@dp.message(Command("postvk"))
@dp.channel_post(Command("postvk"))
async def cmd_post_vk_manual(message: Message):
    await check_and_save_manager(message.from_user)
    try:
        await message.delete()
    except Exception:
        pass

    vk_post_text = (
        "🌲 Добро пожаловать в таежную лавку «Forest Fair — Лесная Ярмарка» (г. Томск)! 🦌\n\n"
        "Мы бережно собираем и заготавливаем для вас натуральные дары томской тайги и Алтая, "
        "а также привозим проверенные сорта ферментированного чая.\n\n"
        "🛒 НАШ ОНЛАЙН-МАГАЗИН\n"
        "🌲 Forest Fair — Лесная Ярмарка\n"
        f"🔗 Перейти в витрину: {WEBAPP_URL}\n"
        "⚖️ Выбор веса, фасовки и оформление заказа онлайн.\n\n"
        "🍄 Что вы найдете в нашей лавке:\n"
        "• Мицелий Ежовика Гребенчатого (Россия) — розница от 100 г и опт от 10 кг для ясного ума и фокуса\n"
        "• Свежая отборная сибирская лисичка 1-го и 2-го сорта\n"
        "• Целебный антипаразитарный сбор таежных трав\n"
        "• Премиальный ферментированный чай: Габба (Рубиновая, Топаз, Бриллиант) и бодрящий Пуэр\n"
        "• Таежные дикоросы правильной бережной заготовки\n\n"
        "🎁 Промокод FOREST10 дает скидку 10% на весь ассортимент!\n\n"
        "🚚 Доставка и самовывоз:\n"
        "• Самовывоз в г. Томск\n"
        "• Доставка по всей России (курьер / ТК). При заказе от 5 000 ₽ — доставка БЕСПЛАТНО!\n"
        "• Оплата: переводом или наличными\n\n"
        f"💬 Напишите нам в [club{VK_GROUP_ID}|сообщения группы] — наш онлайн-консультант "
        "круглосуточно поможет с подбором продукции!\n"
        "👤 Менеджер по заказам: Матвей (+7 (996) 636-45-66 | TG: @MBarukh)"
    )

    final_vk_text = clean_html_for_vk(vk_post_text)
    target_attachment = "photo-236995482_457239107"

    async with aiohttp.ClientSession() as session:
        params = {
            "owner_id": "-" + str(VK_GROUP_ID),
            "from_group": "1",
            "message": final_vk_text,
            "attachments": target_attachment,
        }
        res = await vk_call_api(session, "wall.post", params)

        if res.get("response", {}).get("post_id"):
            post_id = res["response"]["post_id"]
            logging.info("Пост успешно опубликован в ВК через /postvk! ID: %s", post_id)
            await message.answer(f"✅ Пост успешно опубликован на стену ВКонтакте! ID: {post_id}")
        else:
            logging.warning("Не удалось опубликовать пост в ВК: %s", res)
            await message.answer("❌ Ошибка публикации в ВК. Проверьте логи.")

@dp.message(Command("reset_index"))
async def cmd_reset_index(message: Message):
    await check_and_save_manager(message.from_user)
    try:
        with open(BLOG_INDEX_FILE, "w", encoding="utf-8") as f:
            f.write("0")
        await message.answer("🔄 Счетчик обложек блога успешно сброшен на 0 (начнется с первой картинки).")
    except Exception as e:
        await message.answer(f"❌ Ошибка сброса: {e}")

@dp.message(Command("catalog_post"))
@dp.channel_post(Command("catalog_post"))
async def cmd_catalog_post_for_group(message: Message):
    await check_and_save_manager(message.from_user)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(text=CATALOG_TEXT, reply_markup=get_group_post_kb())


@dp.message(Command("review_post"))
@dp.channel_post(Command("review_post"))
async def cmd_post_review_header(message: Message, bot: Bot):
    await check_and_save_manager(message.from_user)
    try:
        await message.delete()
    except Exception:
        pass

    photo = get_review_banner_image()
    thread_id = message.message_thread_id if message.is_topic_message else None

    if photo:
        sent_msg = await bot.send_photo(
            chat_id=message.chat.id,
            message_thread_id=thread_id,
            photo=photo,
            caption=REVIEW_TOPIC_TEXT,
            reply_markup=get_review_topic_kb(),
        )
    else:
        sent_msg = await bot.send_message(
            chat_id=message.chat.id,
            message_thread_id=thread_id,
            text=REVIEW_TOPIC_TEXT,
            reply_markup=get_review_topic_kb(),
            disable_web_page_preview=True,
        )

    if sent_msg:
        try:
            await sent_msg.pin(disable_notification=False)
        except Exception as e:
            logging.warning("Не удалось закрепить отзыв: %s", e)


@dp.message(Command("blog"))
@dp.channel_post(Command("blog"))
async def cmd_publish_blog_now(message: Message, bot: Bot):
    await check_and_save_manager(message.from_user)
    try:
        await message.delete()
    except Exception:
        pass

    article = await generate_blog_article()
    current_idx = get_and_advance_blog_index()

    # Принудительно отделяем витрину переносом строки, если Gemini прилепил её к тексту
    article = re.sub(r'([^\n])\s*(🛒?\s*<a\s+[^>]*>)', r'\1\n\n\2', article)

    # Для Telegram гарантируем внутреннюю ссылку на бота
    tg_article = article.replace(WEBAPP_URL, TG_SHOP_LINK)

    # Для ВКонтакте подменяем ссылку на веб-витрину
    vk_article = article.replace(TG_SHOP_LINK, WEBAPP_URL)

    target_chat = (
        message.chat.id
        if message.chat.type != ChatType.PRIVATE
        else message.from_user.id
    )

    # 1. Отправляем в Telegram
    img_path = get_tg_blog_image_by_index(current_idx)
    if img_path and os.path.exists(img_path):
        photo = FSInputFile(img_path)
        if len(tg_article) <= 1024:
            await bot.send_photo(
                chat_id=target_chat,
                photo=photo,
                caption=tg_article,
                reply_markup=get_blog_post_kb(),
            )
        else:
            await bot.send_photo(chat_id=target_chat, photo=photo)
            await bot.send_message(
                chat_id=target_chat,
                text=tg_article,
                reply_markup=get_blog_post_kb(),
            )
    else:
        await bot.send_message(
            chat_id=target_chat, text=tg_article, reply_markup=get_blog_post_kb()
        )

    # 2. Публикуем в группу ВК напрямую на витрину github.io
    async with aiohttp.ClientSession() as session:
        await publish_vk_wall_post(session, vk_article, current_idx=current_idx)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await check_and_save_manager(message.from_user)
    if message.chat.type != ChatType.PRIVATE:
        try:
            await message.delete()
        except Exception:
            pass
        return

    CHAT_HISTORIES.pop(message.from_user.id, None)

    args = message.text.split(maxsplit=1)
    param = args[1] if len(args) > 1 else ""

    if param == "shop":
        shop_text = (
            "🌲 <b>Добро пожаловать в магазин «Forest Fair — Лесная Ярмарка»!</b> 🌲\n\n"
            "👇 <b>Нажмите на большую кнопку «🛒 Открыть Магазин» в самом низу экрана</b>, "
            "чтобы открыть интерактивную витрину с выбором веса, промокодом и доставкой!\n\n"
            "💬 А если нужна помощь с подбором — просто напишите свой вопрос прямо в этот чат.\n\n"
            "🔗 <b>Контакты:</b>\n"
            "• Менеджер: @MBarukh (Матвей)\n"
            "• 💻 Digital-архитектура: @DJonWeaK"
        )
        await send_photo_fast(message, shop_text, main_kb)
    elif param == "catalog":
        await message.answer(text=CATALOG_TEXT, reply_markup=main_kb)
    elif param == "promo":
        await send_photo_fast(message, PROMO_TEXT, main_kb)
    else:
        await send_photo_fast(message, WELCOME_TEXT, main_kb)


@dp.message(F.web_app_data)
async def handle_webapp_order(message: Message, bot: Bot):
    user = message.from_user
    await check_and_save_manager(user)
    try:
        data = json.loads(message.web_app_data.data)
        full_name = user.full_name or "Покупатель"
        if not data.get("customer_name"):
            data["customer_name"] = full_name

        saved_order = await create_saved_order(data, tg_user_id=user.id)
        order_id = saved_order["order_id"]
        core_order_text = format_order_core_text(data, order_id=order_id)

        username_str = (
            "@" + user.username
            if user.username
            else "не указан (переход по кнопке ниже)"
        )
        user_link = "<a href='tg://user?id=" + str(user.id) + "'>" + full_name + "</a>"

        manager_notification = (
            "🔥 <b>НОВЫЙ ЗАКАЗ #" + order_id + " ИЗ TELEGRAM!</b> 🔥\n\n"
            "👤 <b>Покупатель:</b> " + user_link + "\n"
            "📱 <b>Username:</b> " + username_str + "\n"
            "🆔 <b>ID клиента:</b> <code>" + str(user.id) + "</code>\n"
            "💡 <i>Добавить трек-номер:</i> <code>/track "
            + order_id
            + " ТРЕК</code>\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n" + core_order_text
        )

        kb = get_manager_order_kb(order_id, tg_user_id=user.id, full_name=full_name)
        await send_manager_alerts(bot, manager_notification, kb)

        final_user_text = (
            core_order_text
            + "\n\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
            + "✅ <b>Заказ #"
            + order_id
            + " успешно принят! С вами в ближайшее время свяжется наш менеджер Матвей (@MBarukh).</b>\n\n"
            + "🌲 <i>Спасибо за выбор «Forest Fair»! Приятного вам дня и крепкого сибирского здоровья!</i> 🍃"
        )

        await message.answer(text=final_user_text, reply_markup=main_kb)
        await message.answer(text=GUIDE_TEXT, reply_markup=main_kb)

        await register_customer_order(user.id, full_name)

    except Exception as e:
        logging.error("Ошибка обработки данных магазина: %s", e)
        await message.answer(
            "📦 Заказ получен! Пожалуйста, напишите менеджеру Матвею (@MBarukh) для завершения оформления.",
            reply_markup=main_kb,
        )


@dp.message(F.chat.type == ChatType.PRIVATE, F.text == "🍄 Каталог и цены")
async def show_catalog(message: Message):
    await check_and_save_manager(message.from_user)
    await message.answer(text=CATALOG_TEXT, reply_markup=main_kb)


@dp.message(
    F.chat.type == ChatType.PRIVATE, F.text == "📖 Памятка по приему и завариванию"
)
async def show_guide(message: Message):
    await check_and_save_manager(message.from_user)
    await message.answer(text=GUIDE_TEXT, reply_markup=main_kb)


@dp.message(F.chat.type == ChatType.PRIVATE, F.text == "📦 Оформить заказ")
async def show_order_info(message: Message):
    await check_and_save_manager(message.from_user)
    await message.answer(text=ORDER_TEXT, reply_markup=main_kb)


@dp.message(F.chat.type == ChatType.PRIVATE, F.text == "🎁 Скидка / Промокод")
async def show_promo(message: Message):
    await check_and_save_manager(message.from_user)
    await message.answer(text=PROMO_TEXT, reply_markup=main_kb)


@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def consultant_handler(message: Message, bot: Bot):
    await check_and_save_manager(message.from_user)
    user_id = message.from_user.id
    user_query = message.text

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)

    history = CHAT_HISTORIES.get(user_id, [])
    history.append({"role": "user", "parts": [{"text": user_query}]})
    if len(history) > 12:
        history = history[-12:]

    clean_reply = await call_gemini_rest(history, use_system=True)

    if clean_reply:
        history.append({"role": "model", "parts": [{"text": clean_reply}]})
        CHAT_HISTORIES[user_id] = history
        await message.answer(text=clean_reply, parse_mode=None, reply_markup=main_kb)
        return

    logging.error("Ошибка при обращении к Gemini API.")
    await message.answer(
        "🌿 Сейчас все консультанты немного заняты.\n\n"
        "Вы можете открыть интерактивную витрину по кнопке <b>«🛒 Открыть Магазин»</b> "
        "или написать напрямую менеджеру Матвею: @MBarukh (тел. +7(996)-636-45-66).",
        reply_markup=main_kb,
    )


# =====================================================================
# ВЕБ-ПРИЕМНИК ЗАКАЗОВ (HTTPS)
# =====================================================================
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "https://mikefletcher-byte.github.io",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Vary": "Origin",
}


async def handle_telegram_auth(request: web.Request):
    """Authenticate a Telegram WebApp using Telegram-signed initData."""
    if not TELEGRAM_TOKEN:
        return web.json_response(
            {"ok": False, "error": "telegram_auth_unavailable"},
            status=503,
            headers={**CORS_HEADERS, "Cache-Control": "no-store"},
        )
    try:
        if request.content_length is not None and request.content_length > 16_384:
            raise TelegramInitDataError("initData is too large")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise TelegramInitDataError("invalid request body")
        init_data = payload.get("initData")
        verified_user = verify_telegram_init_data(init_data, TELEGRAM_TOKEN)
        telegram_id = int(verified_user["id"])
        auth_store: AuthStore = request.app["auth_store"]
        user_id, created = auth_store.get_or_create_user_for_identity(
            "telegram", telegram_id
        )
        user_agent = request.headers.get("User-Agent", "")[:200] or None
        session_id, session_token, expires_at = auth_store.create_session(
            user_id, device_label=user_agent
        )
        return web.json_response(
            {
                "ok": True,
                "token": session_token,
                "expires_at": expires_at,
                "user": {"user_id": user_id},
                "account_created": created,
                "session_id": session_id,
            },
            headers={**CORS_HEADERS, "Cache-Control": "no-store"},
        )
    except TelegramInitDataError:
        return web.json_response(
            {"ok": False, "error": "telegram_auth_failed"},
            status=401,
            headers={**CORS_HEADERS, "Cache-Control": "no-store"},
        )
    except InactiveUser:
        return web.json_response(
            {"ok": False, "error": "account_unavailable"},
            status=403,
            headers={**CORS_HEADERS, "Cache-Control": "no-store"},
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response(
            {"ok": False, "error": "invalid_request"},
            status=400,
            headers={**CORS_HEADERS, "Cache-Control": "no-store"},
        )
    except Exception:
        logging.exception("Ошибка Telegram-аутентификации")
        return web.json_response(
            {"ok": False, "error": "authentication_unavailable"},
            status=500,
            headers={**CORS_HEADERS, "Cache-Control": "no-store"},
        )


async def handle_auth_me(request: web.Request):
    """Return the account associated with the presented active session."""
    auth_store: AuthStore = request.app["auth_store"]
    session = get_bearer_session(
        auth_store, request.headers.get("Authorization")
    )
    if not session:
        return web.json_response(
            {"ok": False, "error": "unauthorized"},
            status=401,
            headers={**CORS_HEADERS, "Cache-Control": "no-store"},
        )
    return web.json_response(
        {
            "ok": True,
            "user": {"user_id": session["user_id"]},
            "session": {
                "id": session["session_id"],
                "created_at": session["created_at"],
                "expires_at": session["expires_at"],
                "device_label": session["device_label"],
            },
        },
        headers={**CORS_HEADERS, "Cache-Control": "no-store"},
    )


async def handle_auth_logout(request: web.Request):
    """Revoke the current bearer session."""
    auth_store: AuthStore = request.app["auth_store"]
    session = get_bearer_session(
        auth_store, request.headers.get("Authorization")
    )
    if not session:
        return web.json_response(
            {"ok": False, "error": "unauthorized"},
            status=401,
            headers={**CORS_HEADERS, "Cache-Control": "no-store"},
        )
    revoked = auth_store.revoke_session(
        int(session["user_id"]), int(session["session_id"])
    )
    return web.json_response(
        {"ok": bool(revoked)},
        headers={**CORS_HEADERS, "Cache-Control": "no-store"},
    )


async def handle_options(request: web.Request):
    return web.Response(status=200, headers=CORS_HEADERS)


async def handle_external_order(request: web.Request):
    bot: Bot = request.app["bot"]
    try:
        data = await request.json()
        customer_name = (
            data.get("customer_name", "").strip() or "Покупатель из приложения"
        )
        data["customer_name"] = customer_name
        source_label = data.get("source", "Веб-приложение (PWA / Браузер)")

        saved_order = await create_saved_order(data, tg_user_id=None)
        order_id = saved_order["order_id"]
        core_order_text = format_order_core_text(data, order_id=order_id)

        manager_notification = (
            "🌐 <b>НОВЫЙ ЗАКАЗ #" + order_id + " ИЗ ПРИЛОЖЕНИЯ!</b> 🔥\n\n"
            "👤 <b>Имя покупателя:</b> " + customer_name + "\n"
            "📲 <b>Источник:</b> " + source_label + "\n"
            "💡 <i>Добавить трек-номер:</i> <code>/track "
            + order_id
            + " ТРЕК</code>\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n" + core_order_text
        )

        kb = get_manager_order_kb(order_id, tg_user_id=None, full_name=customer_name)
        await send_manager_alerts(bot, manager_notification, kb)
        return web.json_response(
            {"ok": True, "order": saved_order}, headers=CORS_HEADERS
        )
    except Exception as e:
        logging.error("Ошибка приема внешнего заказа: %s", e)
        return web.json_response(
            {"ok": False, "error": str(e)}, status=400, headers=CORS_HEADERS
        )


async def handle_get_orders(request: web.Request):
    try:
        ids_param = request.query.get("ids", "").strip()
        phone_param = request.query.get("phone", "").strip()
        orders = load_orders()
        result = []

        if ids_param:
            id_list = [x.strip() for x in ids_param.split(",") if x.strip()]
            for oid in reversed(id_list):
                if oid in orders:
                    result.append(orders[oid])
        elif phone_param:
            clean_p = "".join(ch for ch in phone_param if ch.isdigit())[-10:]
            if len(clean_p) >= 6:
                for oid, rec in reversed(list(orders.items())):
                    rec_p = "".join(ch for ch in rec.get("phone", "") if ch.isdigit())[-10:]
                    if clean_p in rec_p:
                        result.append(rec)

        return web.json_response(
            {"ok": True, "orders": result[:15]}, headers=CORS_HEADERS
        )
    except Exception as e:
        return web.json_response(
            {"ok": False, "error": str(e)}, status=400, headers=CORS_HEADERS
        )


async def start_http_api(bot: Bot):
    auth_store = AuthStore(AUTH_DB_FILE)
    auth_store.initialize()
    app = web.Application(client_max_size=16_384)
    app["bot"] = bot
    app["auth_store"] = auth_store
    app.router.add_route("OPTIONS", "/api/auth/telegram", handle_options)
    app.router.add_post("/api/auth/telegram", handle_telegram_auth)
    app.router.add_route("OPTIONS", "/api/auth/me", handle_options)
    app.router.add_get("/api/auth/me", handle_auth_me)
    app.router.add_route("OPTIONS", "/api/auth/logout", handle_options)
    app.router.add_post("/api/auth/logout", handle_auth_logout)
    app.router.add_route("OPTIONS", "/api/order", handle_options)
    app.router.add_post("/api/order", handle_external_order)
    app.router.add_route("OPTIONS", "/api/orders", handle_options)
    app.router.add_get("/api/orders", handle_get_orders)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8080)
    await site.start()
    logging.info("HTTP API запущен на 127.0.0.1:8080")


async def customer_reminders_scheduler(bot: Bot):
    DAY_SECONDS = 86400
    while True:
        await asyncio.sleep(3600)
        reminders = load_reminders()
        if not reminders:
            continue

        now_ts = time.time()
        changed = False

        for item in reminders:
            user_id = item.get("user_id")
            order_ts = item.get("order_ts", now_ts)
            elapsed_days = (now_ts - order_ts) / DAY_SECONDS

            if elapsed_days >= 8 and not item.get("review_sent"):
                review_text = (
                    "🌲 <b>Добрый день от таежной лавки «Forest Fair»!</b> 🌲\n\n"
                    "Прошла неделя с момента вашего заказа. Будем очень признательны, если вы поделитесь впечатлениями!\n\n"
                    "🎁 <b>Напишите пару строк о ваших ощущениях в нашей теме «Отзывы» в группе "
                    "<a href='https://t.me/ForestFair70/290'>Forest Fair — Лесная Ярмарка</a> и получите бонус "
                    "в виде премиального чая при повторном заказе!</b> 🍵"
                )
                review_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✍️ Оставить отзыв (+ чай в подарок)",
                                url="https://t.me/ForestFair70/290",
                            )
                        ]
                    ]
                )
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=review_text,
                        reply_markup=review_kb,
                        disable_web_page_preview=True,
                    )
                except Exception as e:
                    logging.warning("Не удалось отправить отзыв клиенту %s: %s", user_id, e)
                item["review_sent"] = True
                changed = True

            if elapsed_days >= 25 and not item.get("reactivate_sent"):
                reactivate_text = (
                    "🌲 <b>Добрый день! Прошло почти 4 недели с вашего заказа в Forest Fair.</b>\n\n"
                    "Как ваши впечатления от таежных даров? Если ваши запасы подходят к концу, "
                    "желаете ли вы повторить заказ, или подобрать что-то новое?\n\n"
                    "🎁 <b>Также для вас действует бонус постоянного гостя на повторный заказ!</b>\n"
                    "Нажмите кнопку <b>«🛒 Открыть Магазин»</b> внизу или напишите нам прямо в этот чат!"
                )
                try:
                    await bot.send_message(
                        chat_id=user_id, text=reactivate_text, reply_markup=main_kb
                    )
                except Exception as e:
                    logging.warning("Не удалось отправить напоминание %s: %s", user_id, e)
                item["reactivate_sent"] = True
                changed = True

        if changed:
            await save_reminders_async(reminders)


async def auto_blog_scheduler(bot: Bot):
    while True:
        await asyncio.sleep(86400)
        try:
            article = await generate_blog_article()
            current_idx = get_and_advance_blog_index()

            # Принудительно отделяем витрину переносом строки
            article = re.sub(r'([^\n])\s*(🛒?\s*<a\s+[^>]*>)', r'\1\n\n\2', article)

            tg_article = article.replace(WEBAPP_URL, TG_SHOP_LINK)
            vk_article = article.replace(TG_SHOP_LINK, WEBAPP_URL)

            img_path = get_tg_blog_image_by_index(current_idx)

            if img_path and os.path.exists(img_path):
                photo = FSInputFile(img_path)
                if len(tg_article) <= 1024:
                    await bot.send_photo(
                        chat_id=GROUP_USERNAME,
                        photo=photo,
                        caption=tg_article,
                        reply_markup=get_blog_post_kb(),
                    )
                else:
                    await bot.send_photo(chat_id=GROUP_USERNAME, photo=photo)
                    await bot.send_message(
                        chat_id=GROUP_USERNAME,
                        text=tg_article,
                        reply_markup=get_blog_post_kb(),
                    )
            else:
                await bot.send_message(
                    chat_id=GROUP_USERNAME,
                    text=tg_article,
                    reply_markup=get_blog_post_kb(),
                )

            logging.info("Ежедневный пост блога опубликован в Telegram!")

            async with aiohttp.ClientSession() as session:
                await publish_vk_wall_post(session, vk_article, current_idx=current_idx)

        except Exception as e:
            logging.warning("Ошибка авто-публикации блога: %s", e)


async def vk_long_poll_listener(bot: Bot):
    if not VK_TOKEN:
        logging.warning("VK_TOKEN не найден. VK Long Poll отключен.")
        return

    logging.info("Запуск слушателя ВКонтакте Long Poll для группы ID: %s", VK_GROUP_ID)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                server_data = await vk_call_api(
                    session, "groups.getLongPollServer", {"group_id": str(VK_GROUP_ID)}
                )
                resp = server_data.get("response")
                if not resp:
                    logging.warning("Не удалось получить Long Poll сервер ВК. Повтор через 10 сек...")
                    await asyncio.sleep(10)
                    continue

                server = resp.get("server")
                key = resp.get("key")
                ts = resp.get("ts")

                while True:
                    lp_url = server + "?act=a_check&key=" + key + "&ts=" + str(ts) + "&wait=25"
                    try:
                        async with session.get(lp_url, timeout=35) as lp_resp:
                            lp_data = await lp_resp.json()
                    except Exception as e:
                        logging.warning("Таймаут Long Poll ВК: %s", e)
                        await asyncio.sleep(2)
                        break

                    if "failed" in lp_data:
                        failed = lp_data["failed"]
                        if failed == 1:
                            ts = lp_data.get("ts", ts)
                        elif failed in (2, 3):
                            break
                        continue

                    ts = lp_data.get("ts", ts)
                    updates = lp_data.get("updates", [])

                    for upd in updates:
                        upd_type = upd.get("type")
                        if upd_type == "message_new":
                            msg_obj = upd.get("object", {}).get("message", {})
                            from_id = msg_obj.get("from_id")
                            peer_id = msg_obj.get("peer_id")
                            user_text = msg_obj.get("text", "").strip()

                            if not user_text or from_id < 0:
                                continue

                            logging.info("Входящее сообщение ВК от %s: %s", from_id, user_text)

                            history = VK_CHAT_HISTORIES.get(from_id, [])
                            history.append({"role": "user", "parts": [{"text": user_text}]})
                            if len(history) > 12:
                                history = history[-12:]

                            clean_reply = await call_gemini_rest(history, use_system=True)
                            if not clean_reply:
                                clean_reply = (
                                    "Здравствуйте! Рад помочь вам. Подробный каталог с расчетом веса доступен в нашей витрине: "
                                    "mikefletcher-byte.github.io/forest-fair/\n"
                                    "Или напишите менеджеру Матвею: +7(996)-636-45-66 (Telegram: @MBarukh)"
                                )
                            else:
                                history.append({"role": "model", "parts": [{"text": clean_reply}]})
                                VK_CHAT_HISTORIES[from_id] = history

                            vk_text = clean_html_for_vk(clean_reply)
                            send_params = {
                                "peer_id": str(peer_id),
                                "random_id": str(random.randint(1, 2147483647)),
                                "message": vk_text,
                            }
                            await vk_call_api(session, "messages.send", send_params)

                            vk_alert_text = (
                                "💬 <b>НОВОЕ ОБРАЩЕНИЕ ВКОНТАКТЕ!</b> 🌲\n\n"
                                "👤 <b>Клиент:</b> <a href='https://vk.com/id"
                                + str(from_id)
                                + "'>vk.com/id"
                                + str(from_id)
                                + "</a>\n"
                                "❓ <b>Вопрос:</b> "
                                + user_text
                                + "\n\n"
                                "🤖 <b>Ответ консультанта:</b>\n"
                                + clean_reply[:350]
                                + "..."
                            )
                            await send_manager_alerts(bot, vk_alert_text)

            except Exception as e:
                logging.error("Сбой Long Poll ВК: %s", e)
                await asyncio.sleep(5)


async def main():
    bot = Bot(
        token=TELEGRAM_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await update_models_pool()
        await start_http_api(bot)
        await bot.delete_webhook(drop_pending_updates=True)
        me = await bot.get_me()
        logging.info("Telegram-бот @%s успешно запущен!", me.username)

        asyncio.create_task(customer_reminders_scheduler(bot))
        asyncio.create_task(auto_blog_scheduler(bot))
        asyncio.create_task(vk_long_poll_listener(bot))

        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен пользователем.")