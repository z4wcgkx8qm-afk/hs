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
from aiogram.types import Message
from pymax import WebClient, ExtraConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None
processing = set()

def read_qr(image: Image.Image) -> str | None:
    try:
        img = np.array(image.convert('RGB'))
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(img)
        if data: return data
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        data, _, _ = detector.detectAndDecode(otsu)
        if data: return data
        adp = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
        data, _, _ = detector.detectAndDecode(adp)
        if data: return data
        h, w = img.shape[:2]
        scaled2 = cv2.resize(img, (w*2, h*2), interpolation=cv2.INTER_CUBIC)
        data, _, _ = detector.detectAndDecode(scaled2)
        if data: return data
        scaled3 = cv2.resize(img, (w*3, h*3), interpolation=cv2.INTER_CUBIC)
        data, _, _ = detector.detectAndDecode(scaled3)
        if data: return data
        inv = cv2.bitwise_not(img)
        data, _, _ = detector.detectAndDecode(inv)
        if data: return data
        gray_inv = cv2.cvtColor(inv, cv2.COLOR_BGR2GRAY)
        _, otsu_inv = cv2.threshold(gray_inv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        data, _, _ = detector.detectAndDecode(otsu_inv)
        if data: return data
        adp_inv = cv2.adaptiveThreshold(gray_inv, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
        data, _, _ = detector.detectAndDecode(adp_inv)
        if data: return data
        scaled_inv = cv2.resize(inv, (w*2, h*2), interpolation=cv2.INTER_CUBIC)
        data, _, _ = detector.detectAndDecode(scaled_inv)
        if data: return data
        blurred = cv2.GaussianBlur(gray, (0,0), 3)
        sharp = cv2.addWeighted(gray, 1.5, blurred, -0.5, 0)
        _, binary = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        data, _, _ = detector.detectAndDecode(binary)
        if data: return data
        return None
    except:
        return None

def extract_token(text: str) -> str | None:
    m = re.search(r'An_Sx6HQ9HDi[a-zA-Z0-9_\-]+', text)
    return m.group(0) if m else None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as c:
        await c.execute("CREATE TABLE IF NOT EXISTS tokens (id SERIAL PRIMARY KEY, phone TEXT, token TEXT, alive BOOLEAN DEFAULT TRUE)")

async def get_token():
    async with db_pool.acquire() as c:
        r = await c.fetchrow("SELECT id, phone, token FROM tokens WHERE alive=TRUE AND token IS NOT NULL ORDER BY RANDOM() LIMIT 1")
        return {"id": r["id"], "phone": r["phone"], "token": r["token"]} if r else None

async def kill(id): 
    async with db_pool.acquire() as c: await c.execute("UPDATE tokens SET alive=FALSE WHERE id=$1", id)

async def save(tok): 
    async with db_pool.acquire() as c:
        e = await c.fetchval("SELECT id FROM tokens WHERE token=$1", tok)
        if not e: await c.execute("INSERT INTO tokens(token) VALUES($1)", tok)

@dp.message(Command("start"))
async def start(msg: Message):
    if msg.chat.type == "private":
        await msg.answer("📥 Отправь токены (каждый с новой строки) для загрузки.\n\nОтправь QR в любой группе — автоскан.")

@dp.message(F.photo)
async def qr(msg: Message):
    if msg.chat.type not in ("group","supergroup"): return
    if msg.message_id in processing: return
    processing.add(msg.message_id)
    try:
        photo = msg.photo[-1]
        buf = BytesIO()
        await bot.download(photo, destination=buf)
        buf.seek(0)
        img = Image.open(buf)
        qr = read_qr(img)
        if not qr or ("max.ru" not in qr and "oneme.ru" not in qr):
            await msg.reply("❌ QR не распознан" if not qr else "❌ Не MAX")
            return
        tried = 0
        while True:
            t = await get_token()
            if not t:
                await msg.reply("❌ Нет токенов"); return
            tried += 1
            try:
                c = WebClient(work_dir="cache", session_name=f"qr_{t['id']}.db", extra_config=ExtraConfig(token=t['token']))
                task = asyncio.create_task(c.start())
                for _ in range(6):
                    if c.me and c.me.contact: break
                    await asyncio.sleep(0.5)
                if not c.me or not c.me.contact:
                    task.cancel(); await kill(t['id']); continue
                await c.authorize_qr_login(qr)
                task.cancel()
                await msg.reply(f"✅ {c.me.contact.phone if c.me and c.me.contact else 'OK'}")
                return
            except Exception as e:
                err = str(e).lower()
                if "expired" in err:
                    await msg.reply("❌ QR истек"); return
                elif "blocked" in err or "recovery" in err:
                    await kill(t['id']); continue
                elif "connect" in err or "network" in err or "timeout" in err:
                    continue
                else:
                    await kill(t['id']); continue
    except:
        await msg.reply("❌ Ошибка")
    finally:
        processing.discard(msg.message_id)

@dp.message()
async def text(msg: Message):
    if msg.chat.type == "private" and msg.text:
        lines = [l.strip() for l in msg.text.split("\n") if l.strip()]
        tokens = [extract_token(l) for l in lines if extract_token(l)]
        if not tokens: return
        n = 0
        for t in tokens:
            await save(t); n += 1
        await msg.answer(f"✅ {n} токенов")

async def main():
    await init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
