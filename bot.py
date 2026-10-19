import asyncio
import json
import logging
import os
import random
import sys
import time
import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction, ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    FSInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)

# =====================================================================
# КОНФИГУРАЦИЯ И КЛЮЧИ
# =====================================================================
TELEGRAM_TOKEN = "8799364785:AAEoxRpcGbXqk6UNvnGjOVdwhkvfnbIBSjQ"
GEMINI_API_KEY = open("key.txt").read().strip() if os.path.exists("key.txt") else ""
WEBAPP_URL = "https://mikefletcher-byte.github.io/forest-fair/"
MANAGER_USERNAME = "MBarukh"
GROUP_USERNAME = "@ForestFair70"

log_handlers = [logging.FileHandler("bot.log", encoding="utf-8")]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=log_handlers
)

CACHED_LOGO_ID = None
CHAT_HISTORIES = {}
MANAGER_CHAT_ID_FILE = "manager_id.txt"
ADMIN_CHAT_ID_FILE = "admin_id.txt"
REMINDERS_FILE = "reminders.json"


def get_saved_id(filepath: str):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return int(f.read().strip())
        except Exception:
            return None
    return None


def save_id(filepath: str, chat_id: int):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(str(chat_id))
    except Exception as e:
        logging.warning("Не удалось сохранить ID в %s: %s", filepath, e)


def check_and_save_manager(user):
    if not user:
        return
    if user.username and user.username.lower() == MANAGER_USERNAME.lower():
        save_id(MANAGER_CHAT_ID_FILE, user.id)
    elif get_saved_id(ADMIN_CHAT_ID_FILE) is None:
        save_id(ADMIN_CHAT_ID_FILE, user.id)


def load_reminders():
    if os.path.exists(REMINDERS_FILE):
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_reminders(data):
    try:
        with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("Ошибка сохранения reminders.json: %s", e)


def register_customer_order(user_id: int, full_name: str):
    reminders = load_reminders()
    now_ts = time.time()
    reminders.append({
        "user_id": user_id,
        "name": full_name,
        "order_ts": now_ts,
        "review_sent": False,
        "reactivate_sent": False
    })
    save_reminders(reminders)


def get_logo_image():
    for filename in ["logo.jpg", "logo.jpeg", "logo.png"]:
        if os.path.exists(filename):
            return FSInputFile(filename)
    return None


# =====================================================================
# НАСТРОЙКА ПРЯМОГО ЗАПРОСА К GEMINI REST API (КАК В CURL)
# =====================================================================
SYSTEM_PROMPT = """
Ты — вежливый, теплый и экспертный онлайн-консультант магазина таежной продукции и дикоросов «Forest Fair — Лесная Ярмарка» (г. Томск).
Общайся в мужском роде (например: «Я рад помочь», «Подскажу»). Никогда не называй себя искусственным интеллектом, нейросетью или ботом — ты живой эксперт-консультант таежной лавки.

ВАЖНЕЙШИЕ ПРАВИЛА ДИАЛОГА:
1. НЕ ЗДОРОВАЙСЯ ЗАНОВО в каждом сообщении! Если в истории диалога ты уже поздоровался с клиентом, сразу переходи к сути ответа.
2. НЕ ПОВТОРЯЙ в каждом сообщении одни и те же шаблонные абзацы про телефон менеджера и промокод FOREST10, если уже упоминал их ранее.
3. НЕ ИСПОЛЬЗУЙ символы звездочек ** для жирного шрифта. Для списков используй буллиты • или подходящие эмодзи 🌲🍄🍵🌿.
4. В живой речи для соблюдения правил Telegram вместо прямого слова «мухомор» используй точные названия из нашего ассортимента: «Красный колпак», «Пантерный колпак», «Пантерные шляпки» (обрати внимание: красных раскрытых шляпок у нас нет, красный идет только в форме отборных колпаков 1-го и 2-го сорта!).

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
- Оформить заказ можно прямо через кнопку «🛒 Открыть Магазин» внизу экрана.
- Способы получения: Курьером / Доставка (500 ₽, а при заказе от 5 000 ₽ — БЕСПЛАТНО!) или Самовывоз в г. Томск.
- Оплата: Переводом или Наличными.
- Промокод FOREST10 дает скидку 10% на любую продукцию!
- Менеджер: Матвей (@MBarukh), телефон: +7(996)-636-45-66.
- Строго 18+. Продукция не является лекарственным средством.
"""

MODELS_POOL = [
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]


async def call_gemini_rest(contents_list, use_system=True):
    """Прямой HTTP-запрос с заголовком X-goog-api-key (точно как в официальном cURL)"""
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY
    }
    payload = {
        "contents": contents_list,
        "generationConfig": {"temperature": 0.7}
    }
    if use_system:
        payload["systemInstruction"] = {
            "parts": [{"text": SYSTEM_PROMPT}]
        }

    async with aiohttp.ClientSession() as session:
        for model_name in MODELS_POOL:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
            try:
                async with session.post(url, headers=headers, json=payload, timeout=25) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        logging.info("Успешный ответ от модели: %s", model_name)
                        return text.replace("**", "")
                    else:
                        err_body = await resp.text()
                        logging.warning("Модель %s вернула код %s: %s", model_name, resp.status, err_body)
            except Exception as e:
                logging.warning("Ошибка соединения с %s: %s", model_name, e)
            await asyncio.sleep(0.4)
    return None


# =====================================================================
# КЛАВИАТУРЫ И ШАБЛОНЫ
# =====================================================================
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="🛒 Открыть Магазин",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        ],
        [
            KeyboardButton(text="🍄 Каталог и цены"),
            KeyboardButton(text="📖 Памятка по приему и завариванию")
        ],
        [
            KeyboardButton(text="📦 Оформить заказ"),
            KeyboardButton(text="🎁 Скидка / Промокод")
        ]
    ],
    resize_keyboard=True,
    input_field_placeholder="Откройте магазин или задайте вопрос..."
)


def get_group_post_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Открыть Магазин",
                    url="https://t.me/ForestFair70_bot?start=shop"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Консультация и Подбор продукции",
                    url="https://t.me/ForestFair70_bot?start=welcome"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🍄 Посмотреть Каталог и цены",
                    url="https://t.me/ForestFair70_bot?start=catalog"
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
                    url="https://t.me/ForestFair70_bot?start=shop"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Обратная связь / Консультация",
                    url="https://t.me/ForestFair70_bot?start=welcome"
                )
            ]
        ]
    )


WELCOME_TEXT = (
    "🌲 <b>Добро пожаловать в «Forest Fair — Лесная Ярмарка»!</b> 🌲\n\n"
    "Мы находимся в сердце Сибири — в г. <b>Томск</b>, и бережно заготавливаем для вас "
    "таежную продукцию, целебные дикоросы, лесные грибы и премиальные чаи.\n\n"
    "🛒 <b>Нажмите кнопку «Открыть Магазин» внизу экрана</b>, чтобы открыть интерактивную витрину с расчетом веса и оформлением доставки!\n\n"
    "🌿 Или <b>просто напишите любой вопрос прямо в этот чат</b> — мы подробно проконсультируем вас!\n\n"
    "🔗 <b>Наши контакты:</b>\n"
    "• Сообщество: <a href='https://t.me/ForestFair70'>Forest Fair — Лесная Ярмарка</a>\n"
    "• Заказ у менеджера: @MBarukh (Матвей)\n"
    "• 💻 Digital-архитектура: @DJonWeaK (<a href='https://github.com/mikefletcher-byte'>GitHub</a>)"
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

STARTER_POSTS = [
    (
        "🌲 <b>Добро пожаловать в таежную лавку «Forest Fair — Лесная Ярмарка» (г. Томск)!</b> 🦌\n\n"
        "Мы живем и работаем в самом сердце Сибири. Наша главная задача — бережно собирать, сушить и отбирать для вас настоящие дары томской тайги и Алтая, а также привозить проверенные сорта ферментированного чая.\n\n"
        "Что вы всегда найдете в нашей лавке:\n"
        "🍄 Отборные Красные колпаки, Пантерные колпаки и Пантерные шляпки 1-го и 2-го сорта правильной низкотемпературной сушки\n"
        "🧡 Свежую таежную лисичку и душистые травяные сборы\n"
        "🧠 Активный мицелий Ежовика Гребенчатого (в розницу от 100 г и оптом)\n"
        "🍵 Премиальную Габбу (Рубиновая, Топаз, Бриллиант) и бодрящий Пуэр\n\n"
        "🎁 Для всех участников группы действует промокод <b>FOREST10</b> — скидка 10% на любую продукцию!\n"
        "👇 Заглядывайте в нашу интерактивную витрину по кнопке ниже:"
    ),
]

BLOG_TOPICS = [
    "Удивительные дикоросы Сибири и Алтая: как таежные травы помогали сибирским охотникам сохранять выносливость",
    "Влияние утренней разминки, дыхательных практик и контрастного душа на нейропластичность и уровень энергии",
    "История чайной культуры: как ферментированные чаи изменили традиции общения и концентрации внимания на Востоке",
    "Циркадные ритмы и гигиена сна: почему вечерний цифровой детокс и травяной настой восстанавливают нервную систему",
    "Грибное царство в истории человечества: интересные этноботанические и научные факты о лесных грибах",
    "Как прогулки в хвойном лесу (фитонциды сосны и кедра) влияют на иммунитет и снижение уровня кортизола",
]

dp = Dispatcher()


async def send_photo_fast(message: Message, caption: str, reply_markup):
    global CACHED_LOGO_ID

    if CACHED_LOGO_ID:
        try:
            return await message.answer_photo(
                photo=CACHED_LOGO_ID,
                caption=caption,
                reply_markup=reply_markup
            )
        except Exception:
            CACHED_LOGO_ID = None

    photo = get_logo_image()
    if photo:
        sent_msg = await message.answer_photo(
            photo=photo,
            caption=caption,
            reply_markup=reply_markup
        )
        if sent_msg.photo:
            CACHED_LOGO_ID = sent_msg.photo[-1].file_id
        return sent_msg
    else:
        return await message.answer(
            text=caption,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )


async def generate_blog_article() -> str:
    topic = random.choice(BLOG_TOPICS)
    prompt = (
        "Напиши увлекательный, познавательный и достоверный пост для блога нашего Telegram-сообщества «Forest Fair — Лесная Ярмарка» (г. Томск) "
        "на тему: «" + topic + "».\n"
        "Требования:\n"
        "1. Объем: 3–4 емких абзаца, легко читаемых с телефона.\n"
        "2. Тон: теплый, научно-популярный, вдохновляющий на здоровый образ жизни и уважение к природе.\n"
        "3. Не используй символы звездочек ** (пиши чистым текстом с красивыми эмодзи 🌲🌿🍵🏃‍♂️).\n"
        "4. В конце поста мягко предложи читателям поделиться мнением или задать вопрос нашему консультанту по кнопке ниже."
    )
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    reply = await call_gemini_rest(contents, use_system=False)
    if reply:
        return "🌲 РУБРИКА «ЛЕСНОЙ БЛОГ FOREST FAIR» 🌲\n\n" + reply
    return STARTER_POSTS[0]


@dp.message(Command("post"))
@dp.channel_post(Command("post"))
async def cmd_post_for_group(message: Message):
    check_and_save_manager(message.from_user)
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


@dp.message(Command("catalog_post"))
@dp.channel_post(Command("catalog_post"))
async def cmd_catalog_post_for_group(message: Message):
    check_and_save_manager(message.from_user)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(text=CATALOG_TEXT, reply_markup=get_group_post_kb())


@dp.message(Command("blog"))
@dp.channel_post(Command("blog"))
async def cmd_publish_blog_now(message: Message, bot: Bot):
    check_and_save_manager(message.from_user)
    try:
        await message.delete()
    except Exception:
        pass
    article = await generate_blog_article()
    target_chat = message.chat.id if message.chat.type != ChatType.PRIVATE else GROUP_USERNAME
    await bot.send_message(
        chat_id=target_chat,
        text=article,
        reply_markup=get_blog_post_kb()
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    check_and_save_manager(message.from_user)
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
            "• 💻 Digital-архитектура: @DJonWeaK (<a href='https://github.com/mikefletcher-byte'>GitHub</a>)"
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
    check_and_save_manager(user)
    try:
        data = json.loads(message.web_app_data.data)
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

        core_lines = ["🌲 <b>ЗАКАЗ В «FOREST FAIR»</b> 🌲\n"]
        core_lines.append("📦 <b>Состав корзины:</b>")

        for idx, item in enumerate(items, start=1):
            title = item.get("title", "Товар")
            grams = item.get("grams", 0)
            subtotal = item.get("subtotal", 0)
            w_label = str(grams // 1000) + " кг" if (grams >= 1000 and grams % 1000 == 0) else str(grams) + " г"
            core_lines.append("• " + str(idx) + ". <b>" + str(title) + "</b> — " + w_label + " (" + str(subtotal) + " ₽)")

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

        core_order_text = "\n".join(core_lines)

        full_name = user.full_name or "Покупатель"
        username_str = "@" + user.username if user.username else "не указан (переход по кнопке ниже)"
        user_link = "<a href='tg://user?id=" + str(user.id) + "'>" + full_name + "</a>"

        manager_notification = (
            "🔥 <b>НОВЫЙ ЗАКАЗ ИЗ МАГАЗИНА!</b> 🔥\n\n"
            "👤 <b>Покупатель:</b> " + user_link + "\n"
            "📱 <b>Username:</b> " + username_str + "\n"
            "🆔 <b>ID клиента:</b> <code>" + str(user.id) + "</code>\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            + core_order_text
        )

        client_contact_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Написать покупателю (" + full_name + ")",
                        url="tg://user?id=" + str(user.id)
                    )
                ]
            ]
        )

        manager_id = get_saved_id(MANAGER_CHAT_ID_FILE)
        admin_id = get_saved_id(ADMIN_CHAT_ID_FILE)

        if manager_id:
            try:
                await bot.send_message(
                    chat_id=manager_id,
                    text=manager_notification,
                    reply_markup=client_contact_kb
                )
            except Exception as e:
                logging.warning("Не удалось отправить в личку менеджеру (%s): %s", manager_id, e)

        if admin_id and admin_id != manager_id:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=manager_notification,
                    reply_markup=client_contact_kb
                )
            except Exception as e:
                logging.warning("Не удалось отправить копию админу (%s): %s", admin_id, e)

        final_user_text = (
            core_order_text
            + "\n\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
            + "✅ <b>Заказ успешно отправлен! С вами в ближайшее время свяжется наш менеджер Матвей (@MBarukh).</b>\n\n"
            + "🌲 <i>Спасибо за выбор «Forest Fair»! Приятного вам дня и крепкого сибирского здоровья!</i> 🍃"
        )

        await message.answer(
            text=final_user_text,
            reply_markup=main_kb
        )

        await message.answer(
            text=GUIDE_TEXT,
            reply_markup=main_kb
        )

        register_customer_order(user.id, full_name)

    except Exception as e:
        logging.error("Ошибка обработки данных магазина: %s", e)
        await message.answer(
            "📦 Заказ получен! Пожалуйста, напишите менеджеру Матвею (@MBarukh) для завершения оформления.",
            reply_markup=main_kb
        )


@dp.message(F.chat.type == ChatType.PRIVATE, F.text == "🍄 Каталог и цены")
async def show_catalog(message: Message):
    check_and_save_manager(message.from_user)
    await message.answer(text=CATALOG_TEXT, reply_markup=main_kb)


@dp.message(F.chat.type == ChatType.PRIVATE, F.text == "📖 Памятка по приему и завариванию")
async def show_guide(message: Message):
    check_and_save_manager(message.from_user)
    await message.answer(text=GUIDE_TEXT, reply_markup=main_kb)


@dp.message(F.chat.type == ChatType.PRIVATE, F.text == "📦 Оформить заказ")
async def show_order_info(message: Message):
    check_and_save_manager(message.from_user)
    await message.answer(text=ORDER_TEXT, reply_markup=main_kb)


@dp.message(F.chat.type == ChatType.PRIVATE, F.text == "🎁 Скидка / Промокод")
async def show_promo(message: Message):
    check_and_save_manager(message.from_user)
    await message.answer(text=PROMO_TEXT, reply_markup=main_kb)


@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def consultant_handler(message: Message, bot: Bot):
    check_and_save_manager(message.from_user)
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
        await message.answer(
            text=clean_reply,
            parse_mode=None,
            reply_markup=main_kb
        )
        return

    logging.error("Ошибка при обращении к Gemini API после всех попыток.")
    await message.answer(
        "🌿 Сейчас все консультанты немного заняты.\n\n"
        "Вы можете открыть интерактивную витрину по кнопке <b>«🛒 Открыть Магазин»</b> "
        "или написать напрямую менеджеру Матвею: @MBarukh (тел. +7(996)-636-45-66).",
        reply_markup=main_kb
    )


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
                    "🎁 <b>Напишите пару строк о ваших ощущениях в нашу тему «Отзывы» в группе "
                    "<a href='https://t.me/ForestFair70'>Forest Fair — Лесная Ярмарка</a> и получите бонус "
                    "в виде премиального чая при повторном заказе!</b> 🍵"
                )
                try:
                    await bot.send_message(chat_id=user_id, text=review_text, disable_web_page_preview=True)
                except Exception as e:
                    logging.warning("Не удалось отправить запрос отзыва клиенту %s: %s", user_id, e)
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
                    await bot.send_message(chat_id=user_id, text=reactivate_text, reply_markup=main_kb)
                except Exception as e:
                    logging.warning("Не удалось отправить 25-дневное напоминание клиенту %s: %s", user_id, e)
                item["reactivate_sent"] = True
                changed = True

        if changed:
            save_reminders(reminders)


async def auto_blog_scheduler(bot: Bot):
    while True:
        await asyncio.sleep(86400)
        try:
            article = await generate_blog_article()
            await bot.send_message(
                chat_id=GROUP_USERNAME,
                text=article,
                reply_markup=get_blog_post_kb()
            )
            logging.info("Ежедневный пост блога успешно опубликован в группе!")
        except Exception as e:
            logging.warning("Ошибка авто-публикации блога в группу: %s", e)


async def main():
    bot = Bot(
        token=TELEGRAM_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        me = await bot.get_me()
        logging.info("Бот @%s успешно запущен!", me.username)

        asyncio.create_task(customer_reminders_scheduler(bot))
        asyncio.create_task(auto_blog_scheduler(bot))

        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен пользователем.")