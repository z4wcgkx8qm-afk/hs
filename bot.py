import asyncio
import os
import asyncpg
import logging
import numpy as np
import cv2
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from pymax import WebClient, ExtraConfig

# === Логирование ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# === Конфигурация ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None
current_token = {}

# === QR-декодер ===
def read_qr(image: Image.Image) -> str | None:
    """Декодирует QR-код с изображения через OpenCV"""
    try:
        img = np.array(image.convert('RGB'))
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        detector = cv2.QRCodeDetector()
        data, bbox, _ = detector.detectAndDecode(img)
        return data if data else None
    except Exception:
        return None

# === База данных ===
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                phone TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                alive BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
    logger.info("База данных инициализирована")

async def get_random_token():
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT phone, token FROM tokens WHERE alive = TRUE ORDER BY RANDOM() LIMIT 1"
        )
        return {"phone": row["phone"], "token": row["token"]} if row else None

async def save_token(phone: str, token: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tokens (phone, token) VALUES ($1, $2) "
            "ON CONFLICT (phone) DO UPDATE SET token = $2, alive = TRUE, created_at = NOW()",
            phone, token
        )

async def add_token_manual(phone: str, token: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tokens (phone, token) VALUES ($1, $2) "
            "ON CONFLICT (phone) DO UPDATE SET token = $2, alive = TRUE, created_at = NOW()",
            phone, token
        )

# === Команда /start ===
@dp.message(Command("start"))
async def start_cmd(msg: Message):
    await msg.answer(
        "👋 Бот для QR-авторизации MAX\n\n"
        "/get — получить номер для авторизации\n"
        "/add +79161234567 TOKEN — добавить токен вручную\n\n"
        "После /get отправьте скриншот QR-кода с web.max.ru"
    )

# === Команда /add ===
@dp.message(Command("add"))
async def add_cmd(msg: Message):
    parts = msg.text.split()
    if len(parts) != 3:
        return await msg.answer("❌ Используйте: /add +79161234567 ТОКЕН")
    
    phone = parts[1]
    token = parts[2]
    
    if not phone.startswith("+") or len(phone) != 12:
        return await msg.answer("❌ Неверный формат номера. Пример: +79161234567")
    
    await add_token_manual(phone, token)
    logger.info(f"Токен добавлен вручную: {phone}")
    await msg.answer(f"✅ Токен для {phone} добавлен")

# === Команда /get ===
@dp.message(Command("get"))
async def get_cmd(msg: Message):
    token_data = await get_random_token()
    if not token_data:
        return await msg.answer("❌ Нет доступных токенов для авторизации")

    current_token[msg.from_user.id] = token_data
    phone = token_data["phone"]
    msk_time = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M")

    await msg.answer(
        f"📋 Номер: {phone}\n"
        f"🕐 Дата (МСК): {msk_time}\n"
        f"📩 Отправьте QR-код для авторизации этого номера"
    )

# === Приём фото с QR ===
@dp.message(F.photo)
async def qr_handler(msg: Message):
    if msg.from_user.id not in current_token:
        return await msg.answer("❌ Сначала запросите номер командой /get")

    photo = msg.photo[-1]
    buf = BytesIO()
    await bot.download(photo, destination=buf)
    buf.seek(0)

    try:
        img = Image.open(buf)
        qr_data = read_qr(img)
        if not qr_data:
            return await msg.answer("❌ Не удалось распознать QR-код на изображении")
        if "max.ru" not in qr_data and "oneme.ru" not in qr_data:
            return await msg.answer("❌ QR-код не содержит ссылку MAX")
    except Exception:
        return await msg.answer("❌ Не удалось распознать QR-код на изображении")

    token_data = current_token.pop(msg.from_user.id)
    phone = token_data["phone"]
    token = token_data["token"]

    status_msg = await msg.answer(f"⏳ Авторизую {phone}...")

    try:
        web_client = WebClient(
            work_dir="cache",
            session_name=f"qr_{phone}.db",
            extra_config=ExtraConfig(token=token),
        )
        await web_client.start()
        await web_client.authorize_qr_login(qr_data)

        new_token = web_client.token
        if new_token:
            await save_token(phone, new_token)

        await status_msg.edit_text(f"✅ Номер {phone} успешно авторизован")
        logger.info(f"QR-авторизация успешна: {phone}")
    except Exception as e:
        error_text = str(e).lower()
        if "expired" in error_text:
            await status_msg.edit_text("❌ Время действия QR-кода истекло")
        elif "blocked" in error_text:
            await status_msg.edit_text("❌ Номер заблокирован или удалён")
        elif "connect" in error_text or "network" in error_text or "timeout" in error_text:
            await status_msg.edit_text("❌ Проблемы с сетью. Попробуйте позже")
        else:
            await status_msg.edit_text("❌ Не удалось авторизовать номер. Проверьте QR-код")
        logger.warning(f"Ошибка QR-авторизации {phone}: {error_text}")

async def main():
    await init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
