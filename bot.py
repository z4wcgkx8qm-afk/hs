import asyncio
import os
import asyncpg
import logging
import re
import pyrxing
from io import BytesIO
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from pymax import Client, ExtraConfig

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
        results = pyrxing.read_barcodes(image.convert('L'))
        return results[0].text if results else None
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
        await msg.answer(
            "Привет! Это автоскан QR для MAX.\n\n"
            "Отправь /add и токены для загрузки.\n"
            "В любой группе просто отправь QR — я всё сделаю."
        )

@dp.message(Command("add"))
async def add_cmd(msg: Message):
    if msg.chat.type != "private": return
    await msg.answer("📥 Отправь токены. Каждый с новой строки.")

@dp.message()
async def text(msg: Message):
    if msg.chat.type == "private" and msg.text:
        lines = [l.strip() for l in msg.text.split("\n") if l.strip()]
        tokens = [extract_token(l) for l in lines if extract_token(l)]
        if not tokens: return
        n = 0
        for t in tokens:
            await save(t)
            n += 1
        await msg.answer(
            f"Готово. Загружено токенов: {n}\n\n"
            f"Теперь отправь QR в любую группу — я авторизую."
        )

@dp.message(F.photo)
async def qr(msg: Message):
    if msg.chat.type not in ("group", "supergroup"): return
    if msg.message_id in processing: return
    processing.add(msg.message_id)

    try:
        photo = msg.photo[-1]
        buf = BytesIO()
        await bot.download(photo, destination=buf)
        buf.seek(0)
        img = Image.open(buf)
        qr_data = read_qr(img)

        if not qr_data:
            await msg.reply("Не удалось распознать QR-код.\nУбедись, что скриншот чёткий и содержит QR с web.max.ru")
            return

        if "max.ru" not in qr_data and "oneme.ru" not in qr_data:
            await msg.reply("Это не QR-код от MAX.\nНужен скриншот с web.max.ru")
            return

        tried = 0
        while True:
            t = await get_token()
            if not t:
                await msg.reply("Нет доступных токенов для авторизации.\nЗагрузи новые через /add в личные сообщения.")
                return

            tried += 1
            try:
                c = Client(
                    phone="+79990000000",
                    work_dir="cache",
                    session_name=f"qr_{t['id']}.db",
                    extra_config=ExtraConfig(token=t['token'])
                )
                task = asyncio.create_task(c.start())
                for _ in range(6):
                    if c.me and c.me.contact: break
                    await asyncio.sleep(0.5)
                if not c.me or not c.me.contact:
                    task.cancel()
                    await kill(t['id'])
                    continue

                await c.authorize_qr_login(qr_data)
                task.cancel()

                phone = c.me.contact.phone if c.me and c.me.contact else "неизвестен"
                await msg.reply(f"Номер {phone} успешно авторизован.\nТокен обработан.")
                return

            except Exception as e:
                err = str(e).lower()
                if "expired" in err:
                    await msg.reply(
                        "QR-код устарел.\n"
                        "Сделай новый скриншот с web.max.ru и отправь снова."
                    )
                    return
                elif "blocked" in err or "recovery" in err:
                    await kill(t['id'])
                    continue
                elif "connect" in err or "network" in err or "timeout" in err:
                    continue
                else:
                    await kill(t['id'])
                    continue

    except Exception:
        await msg.reply(
            "Произошла ошибка при обработке.\n"
            "Попробуй ещё раз или проверь токены."
        )
    finally:
        processing.discard(msg.message_id)

async def main():
    await init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
