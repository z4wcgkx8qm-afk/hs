
import asyncio
import os
import asyncpg
import logging
import numpy as np
import cv2
import requests
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymax import WebClient, ExtraConfig

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
current_token = {}
expecting_tokens = set()

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

# === База данных ===
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS approved_groups (
                group_id BIGINT PRIMARY KEY
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                phone TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                alive BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
    logger.info("База данных инициализирована")

async def is_group_approved(group_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM approved_groups WHERE group_id = $1", group_id)
        return row is not None

async def add_approved_group(group_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO approved_groups (group_id) VALUES ($1) ON CONFLICT DO NOTHING", group_id)

async def remove_approved_group(group_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM approved_groups WHERE group_id = $1", group_id)

async def get_random_token():
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT phone, token FROM tokens WHERE alive = TRUE ORDER BY RANDOM() LIMIT 1")
        return {"phone": row["phone"], "token": row["token"]} if row else None

async def save_token(phone: str, token: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tokens (phone, token) VALUES ($1, $2) "
            "ON CONFLICT (phone) DO UPDATE SET token = $2, alive = TRUE, created_at = NOW()",
            phone, token
        )

async def delete_dead_tokens():
    async with db_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM tokens WHERE alive = FALSE")
        return int(result.split()[-1]) if result else 0

# === Проверка доступа ===
async def check_access(msg: Message) -> bool:
    if msg.chat.type == "private":
        return msg.from_user.id == ADMIN_ID
    if msg.chat.type in ("group", "supergroup"):
        return await is_group_approved(msg.chat.id)
    return False

# === Команда /start ===
@dp.message(Command("start"))
async def start_cmd(msg: Message):
    if msg.chat.type == "private" and msg.from_user.id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Загрузить аккаунты", callback_data="load_accounts")],
            [InlineKeyboardButton(text="Очистить мёртвые сессии", callback_data="clear_dead_admin")]
        ])
        await msg.answer("🔐 Админ-панель maxPLUS\n\nДобро пожаловать в панель управления ботом.", reply_markup=keyboard)
    elif msg.chat.type == "private":
        return
    else:
        return

# === Команда /setup ===
@dp.message(Command("setup"))
async def setup_cmd(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    try:
        group_id = int(msg.text.split()[1])
        await add_approved_group(group_id)
        await msg.answer(f"✅ Группа {group_id} одобрена")
        logger.info(f"Группа {group_id} одобрена админом")
    except (IndexError, ValueError):
        await msg.answer("❌ Используйте: /setup ID_группы")

# === Команда /unsetup ===
@dp.message(Command("unsetup"))
async def unsetup_cmd(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    try:
        group_id = int(msg.text.split()[1])
        await remove_approved_group(group_id)
        await msg.answer(f"✅ Группа {group_id} удалена из одобренных")
        logger.info(f"Группа {group_id} удалена админом")
    except (IndexError, ValueError):
        await msg.answer("❌ Используйте: /unsetup ID_группы")

# === Команда /get ===
@dp.message(Command("get"))
async def get_cmd(msg: Message):
    if not await check_access(msg):
        return

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

# === Команда /cancel ===
@dp.message(Command("cancel"))
async def cancel_cmd(msg: Message):
    expecting_tokens.discard(msg.from_user.id)
    if msg.from_user.id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Загрузить аккаунты", callback_data="load_accounts")],
            [InlineKeyboardButton(text="Очистить мёртвые сессии", callback_data="clear_dead_admin")]
        ])
        await msg.answer("🔐 Админ-панель maxPLUS\n\nДобро пожаловать в панель управления ботом.", reply_markup=keyboard)

# === Callback: Загрузить аккаунты ===
@dp.callback_query(lambda c: c.data == "load_accounts")
async def load_accounts_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Доступ запрещён", show_alert=True)
    
    expecting_tokens.add(callback.from_user.id)
    await callback.message.answer(
        "📥 Отправьте токены для загрузки.\n"
        "Формат: +79161234567 ТОКЕН\n"
        "Каждая пара с новой строки\n"
        "/cancel — отмена"
    )
    await callback.answer()

# === Callback: Очистить мёртвые ===
@dp.callback_query(lambda c: c.data == "clear_dead_admin")
async def clear_dead_admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Доступ запрещён", show_alert=True)
    
    count = await delete_dead_tokens()
    await callback.message.answer(f"✅ Удалено {count} мёртвых сессий")
    await callback.answer()

# === Приём токенов от админа ===
@dp.message()
async def text_handler(msg: Message):
    if msg.text and msg.text.startswith("/"):
        return

    # Загрузка токенов админом
    if msg.from_user.id in expecting_tokens and msg.from_user.id == ADMIN_ID:
        lines = msg.text.strip().split("\n")
        loaded = 0
        for line in lines:
            parts = line.strip().split()
            if len(parts) == 2 and parts[0].startswith("+") and len(parts[0]) == 12:
                phone = parts[0]
                token = parts[1]
                await save_token(phone, token)
                loaded += 1
        
        expecting_tokens.discard(msg.from_user.id)
        await msg.answer(f"✅ Загружено {loaded} токенов")
        logger.info(f"Админ загрузил {loaded} токенов")
        return

# === Приём фото с QR ===
@dp.message(F.photo)
async def qr_handler(msg: Message):
    if not await check_access(msg):
        return

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
