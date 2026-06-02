import asyncio
import os
import asyncpg
import logging
import re
import numpy as np
import cv2
from io import BytesIO
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymax import Client, ExtraConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = {8540562276, 7742243877, 8706712229}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None
expecting_tokens = set()
processing_tokens = set()

def read_qr(image: Image.Image) -> str | None:
    try:
        img = np.array(image.convert('RGB'))
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        detector = cv2.QRCodeDetector()
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        avg_brightness = np.mean(gray)
        
        if avg_brightness < 128:
            inv = cv2.bitwise_not(img)
            data, _, _ = detector.detectAndDecode(inv)
            if data: return data
            gray_inv = cv2.cvtColor(inv, cv2.COLOR_BGR2GRAY)
            _, otsu = cv2.threshold(gray_inv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            data, _, _ = detector.detectAndDecode(otsu)
            if data: return data
            kernel = np.ones((3,3), np.uint8)
            eroded = cv2.erode(inv, kernel, iterations=1)
            data, _, _ = detector.detectAndDecode(eroded)
            if data: return data
            dilated = cv2.dilate(inv, kernel, iterations=1)
            data, _, _ = detector.detectAndDecode(dilated)
            if data: return data
        else:
            data, _, _ = detector.detectAndDecode(img)
            if data: return data
            _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            data, _, _ = detector.detectAndDecode(otsu)
            if data: return data
            adp = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
            data, _, _ = detector.detectAndDecode(adp)
            if data: return data
            h, w = img.shape[:2]
            scaled = cv2.resize(img, (w*2, h*2), interpolation=cv2.INTER_CUBIC)
            data, _, _ = detector.detectAndDecode(scaled)
            if data: return data
        return None
    except:
        return None

def extract_token(text: str) -> str | None:
    match = re.search(r'An_Sx6HQ9HDi[a-zA-Z0-9_\-]+', text)
    return match.group(0) if match else None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS tokens (id SERIAL PRIMARY KEY, phone TEXT, token TEXT, alive BOOLEAN DEFAULT TRUE, created_at TIMESTAMP DEFAULT NOW())")
    logger.info("База данных инициализирована")

async def get_random_alive_token():
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, phone, token FROM tokens WHERE alive = TRUE AND token IS NOT NULL ORDER BY RANDOM() LIMIT 1")
        return {"id": row["id"], "phone": row["phone"], "token": row["token"]} if row else None

async def save_token_to_db(raw_token: str) -> bool:
    async with db_pool.acquire() as conn:
        existing = await conn.fetchval("SELECT id FROM tokens WHERE token = $1", raw_token)
        if not existing:
            await conn.execute("INSERT INTO tokens (token) VALUES ($1)", raw_token)
            return True
        return False

async def update_token_phone(token_id: int, phone: str):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE tokens SET phone = $1 WHERE id = $2", phone, token_id)

async def mark_token_dead(token_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE tokens SET alive = FALSE WHERE id = $1", token_id)

async def delete_dead_tokens():
    async with db_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM tokens WHERE alive = FALSE")
        return int(result.split()[-1]) if result else 0

async def get_token_counts():
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM tokens")
        alive = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE alive = TRUE")
        return total, alive

def admin_panel_text(total, alive):
    return f"📊 Токенов: {total}\n🟢 Доступно: {alive}\n🔴 Мёртвых: {total - alive}"

def admin_panel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Загрузить токены", callback_data="load_tokens")],
        [InlineKeyboardButton(text="Очистить мёртвые сессии", callback_data="clear_dead_admin")]
    ])

@dp.message(Command("start"))
async def start_cmd(msg: Message):
    if msg.chat.type == "private" and msg.from_user.id in ADMIN_IDS:
        total, alive = await get_token_counts()
        await msg.answer(admin_panel_text(total, alive), reply_markup=admin_panel_keyboard())

@dp.message(Command("cancel"))
async def cancel_cmd(msg: Message):
    expecting_tokens.discard(msg.from_user.id)
    if msg.from_user.id in ADMIN_IDS and msg.chat.type == "private":
        total, alive = await get_token_counts()
        await msg.answer(admin_panel_text(total, alive), reply_markup=admin_panel_keyboard())

@dp.message(Command("nums"))
async def nums_cmd(msg: Message):
    if msg.chat.type in ("group", "supergroup"):
        _, alive = await get_token_counts()
        await msg.answer(f"📊 Доступно токенов: {alive}")

@dp.callback_query(lambda c: c.data == "load_tokens")
async def load_tokens_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Доступ запрещён", show_alert=True)
    expecting_tokens.add(callback.from_user.id)
    await callback.message.answer("📥 Отправьте токены. Каждый с новой строки.\n/cancel — отмена")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "clear_dead_admin")
async def clear_dead_admin_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Доступ запрещён", show_alert=True)
    count = await delete_dead_tokens()
    await callback.answer("Нет мёртвых сессий" if count == 0 else f"Очищено {count} мёртвых сессий", show_alert=True)
    total, alive = await get_token_counts()
    await callback.message.edit_text(admin_panel_text(total, alive), reply_markup=admin_panel_keyboard())

@dp.message()
async def unified_handler(msg: Message):
    if msg.text and msg.text.startswith("/"): return

    if msg.chat.type == "private" and msg.from_user.id in ADMIN_IDS and msg.from_user.id in expecting_tokens:
        if msg.text:
            lines = [line.strip() for line in msg.text.strip().split("\n") if line.strip()]
            tokens = [extract_token(line) for line in lines if extract_token(line)]
            if not tokens:
                await msg.answer("❌ Неверный формат.")
                return
            loaded = 0
            for t in tokens:
                if await save_token_to_db(t):
                    loaded += 1
            skipped = len(tokens) - loaded
            expecting_tokens.discard(msg.from_user.id)
            if loaded and skipped: await msg.answer(f"✅ Загружено {loaded}, {skipped} уже были в базе")
            elif loaded: await msg.answer(f"✅ Загружено {loaded} токенов")
            else: await msg.answer(f"⚠️ Все {skipped} уже были в базе")
        return

    if msg.chat.type in ("group", "supergroup") and msg.photo:
        if msg.message_id not in processing_tokens:
            processing_tokens.add(msg.message_id)
            asyncio.create_task(process_qr(msg))

async def process_qr(msg: Message):
    client = None
    last_error = None
    try:
        photo = msg.photo[-1]
        buf = BytesIO()
        await bot.download(photo, destination=buf)
        buf.seek(0)
        try:
            img = Image.open(buf)
            qr_data = read_qr(img)
        except Exception:
            await msg.reply("❌ Не удалось распознать QR-код"); return
        if not qr_data:
            await msg.reply("❌ Не удалось распознать QR-код"); return
        if "max.ru" not in qr_data and "oneme.ru" not in qr_data:
            await msg.reply("❌ QR-код не содержит ссылку MAX"); return

        tried = 0
        while True:
            token_data = await get_random_alive_token()
            if not token_data:
                await msg.reply(f"❌ {last_error}" if last_error else "❌ Нет доступных токенов")
                return
            tried += 1
            token_id, token, phone = token_data["id"], token_data["token"], token_data["phone"]
            try:
                client = Client(phone="+79990000000", work_dir="cache", session_name=f"qr_{token_id}.db", extra_config=ExtraConfig(token=token))
                start_task = asyncio.create_task(client.start())
                for _ in range(6):
                    if client.me and client.me.contact:
                        break
                    await asyncio.sleep(0.5)
                if not client.me or not client.me.contact:
                    start_task.cancel()
                    await mark_token_dead(token_id)
                    last_error = "Токены недействительны"
                    continue
                await client.authorize_qr_login(qr_data)
                start_task.cancel()
                if not phone:
                    try:
                        if client.me and client.me.contact:
                            phone = client.me.contact.phone
                            await update_token_phone(token_id, phone)
                    except Exception:
                        pass
                await msg.reply(f"✅ Номер {phone or 'неизвестен'} успешно авторизован")
                return
            except Exception as e:
                error_text = str(e).lower()
                if "expired" in error_text:
                    await msg.reply("❌ Время действия QR-кода истекло"); return
                elif "blocked" in error_text or "recovery" in error_text:
                    await mark_token_dead(token_id)
                    last_error = "Номер заблокирован"
                    continue
                elif "connect" in error_text or "network" in error_text or "timeout" in error_text:
                    last_error = "Проблемы с сетью"
                    continue
                else:
                    await mark_token_dead(token_id)
                    last_error = "Токены недействительны"
                    continue
    except Exception:
        await msg.reply("❌ Ошибка обработки")
    finally:
        processing_tokens.discard(msg.message_id)
        if client:
            try:
                await client.close()
            except Exception:
                pass

async def main():
    await init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
