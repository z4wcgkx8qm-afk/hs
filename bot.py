import asyncio
import os
import asyncpg
import logging
import random
import re
import numpy as np
import cv2
import requests
from io import BytesIO
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymax import WebClient, ExtraConfig
from pymax.api.session.payloads import MobileUserAgentPayload
from pymax.api.session.enums import DeviceType

# === Логирование ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# === Конфигурация ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 8540562276

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None
expecting_tokens = set()
processing_tokens = set()

# === Пул WEB user-agent'ов ===
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
]

def get_random_user_agent():
    return MobileUserAgentPayload(
        device_type=DeviceType.WEB,
        app_version="25.12.13",
        os_version="14.0",
        timezone="Europe/Moscow",
        screen="1920x1080",
        device_name="Unknown",
        device_locale="ru",
        header_user_agent=random.choice(USER_AGENTS),
    )

# === QR-декодер ===
def read_qr(image: Image.Image) -> str | None:
    try:
        img = np.array(image.convert('RGB'))
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        detector = cv2.QRCodeDetector()

        data, bbox, _ = detector.detectAndDecode(img)
        if data: return data

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, enhanced = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        data, bbox, _ = detector.detectAndDecode(enhanced)
        if data: return data

        h, w = img.shape[:2]
        scaled = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        data, bbox, _ = detector.detectAndDecode(scaled)
        if data: return data

        scaled3 = cv2.resize(img, (w * 3, h * 3), interpolation=cv2.INTER_CUBIC)
        data, bbox, _ = detector.detectAndDecode(scaled3)
        if data: return data

        blurred = cv2.GaussianBlur(gray, (0, 0), 3)
        sharpened = cv2.addWeighted(gray, 1.5, blurred, -0.5, 0)
        _, binary = cv2.threshold(sharpened, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        data, bbox, _ = detector.detectAndDecode(binary)
        if data: return data

        try:
            buf = BytesIO()
            image.save(buf, format='PNG')
            buf.seek(0)
            files = {'file': ('qr.png', buf, 'image/png')}
            response = requests.post('https://api.qrserver.com/v1/read-qr-code/', files=files, timeout=10)
            if response.status_code == 200:
                result = response.json()
                if result and result[0]['symbol'][0]['data']:
                    return result[0]['symbol'][0]['data']
        except Exception:
            pass

        return None
    except Exception:
        return None

# === Извлечение токена из любого формата ===
def extract_token(text: str) -> str | None:
    match = re.search(r'An_Sx6HQ9HDi[a-zA-Z0-9_\-]+', text)
    if match:
        return match.group(0)
    return None

# === База данных ===
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                id SERIAL PRIMARY KEY,
                phone TEXT,
                token TEXT,
                web_token TEXT UNIQUE,
                alive BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
    logger.info("База данных инициализирована")

# === ВРЕМЕННАЯ КОМАНДА ДЛЯ ОЧИСТКИ БАЗЫ ===
@dp.message(Command("cleardb"))
async def cleardb_cmd(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS tokens CASCADE")
    await init_db()
    await msg.answer("✅ База очищена")
# === УДАЛИТЬ ПОСЛЕ ИСПОЛЬЗОВАНИЯ ===

async def get_random_alive_token():
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, phone, web_token FROM tokens WHERE alive = TRUE AND web_token IS NOT NULL ORDER BY RANDOM() LIMIT 1"
        )
        if row:
            return {"id": row["id"], "phone": row["phone"], "token": row["web_token"], "type": "WEB"}

        row = await conn.fetchrow(
            "SELECT id, phone, token FROM tokens WHERE alive = TRUE AND token IS NOT NULL ORDER BY RANDOM() LIMIT 1"
        )
        if row:
            return {"id": row["id"], "phone": row["phone"], "token": row["token"], "type": "DESKTOP"}

        return None

async def save_web_token(token_id: int, phone: str, web_token: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE tokens SET phone = $1, web_token = $2, alive = TRUE WHERE id = $3",
            phone, web_token, token_id
        )

async def save_token_to_db(raw_token: str):
    async with db_pool.acquire() as conn:
        existing = await conn.fetchval("SELECT id FROM tokens WHERE token = $1", raw_token)
        if not existing:
            await conn.execute("INSERT INTO tokens (token) VALUES ($1)", raw_token)

async def mark_token_dead(token_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE tokens SET alive = FALSE WHERE id = $1", token_id)

async def delete_dead_tokens():
    async with db_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM tokens WHERE alive = FALSE")
        return int(result.split()[-1]) if result else 0

# === Команда /start ===
@dp.message(Command("start"))
async def start_cmd(msg: Message):
    if msg.chat.type == "private" and msg.from_user.id == ADMIN_ID:
        total = 0
        alive = 0
        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM tokens")
            alive = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE alive = TRUE")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Загрузить токены", callback_data="load_tokens")],
            [InlineKeyboardButton(text="Очистить мёртвые сессии", callback_data="clear_dead_admin")]
        ])
        await msg.answer(
            f"🔐 Админ-панель maxPLUS\n\n"
            f"📊 Токенов в базе: {total} (живых: {alive})\n\n"
            f"Добро пожаловать в панель управления ботом.",
            reply_markup=keyboard
        )

# === Команда /cancel ===
@dp.message(Command("cancel"))
async def cancel_cmd(msg: Message):
    expecting_tokens.discard(msg.from_user.id)
    if msg.from_user.id == ADMIN_ID and msg.chat.type == "private":
        total = 0
        alive = 0
        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM tokens")
            alive = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE alive = TRUE")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Загрузить токены", callback_data="load_tokens")],
            [InlineKeyboardButton(text="Очистить мёртвые сессии", callback_data="clear_dead_admin")]
        ])
        await msg.answer(
            f"🔐 Админ-панель maxPLUS\n\n"
            f"📊 Токенов в базе: {total} (живых: {alive})\n\n"
            f"Добро пожаловать в панель управления ботом.",
            reply_markup=keyboard
        )

# === Callback: Загрузить токены ===
@dp.callback_query(lambda c: c.data == "load_tokens")
async def load_tokens_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Доступ запрещён", show_alert=True)

    expecting_tokens.add(callback.from_user.id)
    await callback.message.answer(
        "📥 Отправьте токены для загрузки.\n"
        "Можно кидать в любом формате:\n"
        "- Только токен\n"
        "- JSON из localStorage\n"
        "- Файл с метаданными\n\n"
        "Каждый токен с новой строки\n"
        "/cancel — отмена"
    )
    await callback.answer()

# === Callback: Очистить мёртвые ===
@dp.callback_query(lambda c: c.data == "clear_dead_admin")
async def clear_dead_admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Доступ запрещён", show_alert=True)

    count = await delete_dead_tokens()
    if count == 0:
        await callback.answer("Нет мёртвых сессий", show_alert=True)
    else:
        await callback.answer(f"Очищено {count} мёртвых сессий", show_alert=True)

# === Единый обработчик сообщений ===
@dp.message()
async def unified_handler(msg: Message):
    # Пропускаем команды
    if msg.text and msg.text.startswith("/"):
        return

    # Админ в личке: загрузка токенов
    if msg.chat.type == "private" and msg.from_user.id == ADMIN_ID and msg.from_user.id in expecting_tokens:
        if msg.text:
            lines = msg.text.strip().split("\n")
            loaded = 0
            for line in lines:
                token = extract_token(line.strip())
                if token:
                    await save_token_to_db(token)
                    loaded += 1
            expecting_tokens.discard(msg.from_user.id)
            await msg.answer(f"✅ Загружено {loaded} токенов")
            logger.info(f"Админ загрузил {loaded} токенов")
        return

    # Группа: фото с QR
    if msg.chat.type in ("group", "supergroup") and msg.photo:
        if msg.message_id not in processing_tokens:
            processing_tokens.add(msg.message_id)
            asyncio.create_task(process_qr(msg))
        return

async def process_qr(msg: Message):
    try:
        token_data = await get_random_alive_token()
        if not token_data:
            await msg.reply("❌ Нет доступных токенов")
            return

        photo = msg.photo[-1]
        buf = BytesIO()
        await bot.download(photo, destination=buf)
        buf.seek(0)

        try:
            img = Image.open(buf)
            qr_data = read_qr(img)
        except Exception:
            await msg.reply("❌ Не удалось распознать QR-код")
            return

        if not qr_data:
            await msg.reply("❌ Не удалось распознать QR-код")
            return

        if "max.ru" not in qr_data and "oneme.ru" not in qr_data:
            await msg.reply("❌ QR-код не содержит ссылку MAX")
            return

        token_id = token_data["id"]
        token = token_data["token"]
        token_type = token_data["type"]
        phone = token_data["phone"]

        user_agent = get_random_user_agent()

        if token_type == "DESKTOP":
            web_client = WebClient(
                work_dir="cache",
                session_name=f"qr_{token_id}.db",
                extra_config=ExtraConfig(token=token, user_agent=user_agent),
            )
            await web_client.start()
            new_web_token = web_client.token
            if new_web_token:
                token = new_web_token
        else:
            web_client = WebClient(
                work_dir="cache",
                session_name=f"qr_{token_id}.db",
                extra_config=ExtraConfig(user_agent=user_agent),
            )

        await web_client.authorize_qr_login(qr_data)

        if not phone and web_client.me and web_client.me.contact:
            phone = web_client.me.contact.phone

        new_web_token = web_client.token
        if new_web_token:
            await save_web_token(token_id, phone, new_web_token)

        phone_display = phone or "неизвестен"
        await msg.reply(f"✅ Номер {phone_display} успешно авторизован")
        logger.info(f"QR-авторизация успешна: токен #{token_id}, номер: {phone_display}")
    except Exception as e:
        if token_data:
            await mark_token_dead(token_data["id"])
        error_text = str(e).lower()
        if "expired" in error_text:
            await msg.reply("❌ Время действия QR-кода истекло")
        elif "blocked" in error_text:
            await msg.reply("❌ Номер заблокирован или удалён")
        elif "connect" in error_text or "network" in error_text or "timeout" in error_text:
            await msg.reply("❌ Проблемы с сетью")
        else:
            await msg.reply("❌ Токен недействителен")
        logger.warning(f"Ошибка QR-авторизации токен #{token_data['id'] if token_data else '?'}: {error_text}")
    finally:
        processing_tokens.discard(msg.message_id)

async def main():
    await init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
