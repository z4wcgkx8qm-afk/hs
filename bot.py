import asyncio
import os
import asyncpg
import logging
import re
import pyrxing
from datetime import datetime
from zoneinfo import ZoneInfo
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
        await c.execute("CREATE TABLE IF NOT EXISTS approved_groups (group_id BIGINT PRIMARY KEY)")
        await c.execute("CREATE TABLE IF NOT EXISTS tokens (id SERIAL PRIMARY KEY, phone TEXT, token TEXT, alive BOOLEAN DEFAULT TRUE)")
        await c.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                action TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

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

async def log_stat(user_id, username, action):
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO stats(user_id, username, action) VALUES($1,$2,$3)", user_id, username, action)

async def get_counts():
    async with db_pool.acquire() as c:
        total = await c.fetchval("SELECT COUNT(*) FROM tokens")
        alive = await c.fetchval("SELECT COUNT(*) FROM tokens WHERE alive=TRUE")
        return total, alive

async def is_group_approved(gid):
    async with db_pool.acquire() as c:
        return await c.fetchval("SELECT 1 FROM approved_groups WHERE group_id=$1", gid)

async def add_group(gid):
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO approved_groups VALUES($1) ON CONFLICT DO NOTHING", gid)

async def get_today_stats():
    async with db_pool.acquire() as c:
        rows = await c.fetch("SELECT username, action FROM stats WHERE created_at::date = CURRENT_DATE")
        return rows

@dp.message(Command("start"))
async def start(msg: Message):
    if msg.chat.type == "private":
        total, alive = await get_counts()
        success = await db_pool.fetchval("SELECT COUNT(*) FROM stats WHERE action='success'")
        fail = await db_pool.fetchval("SELECT COUNT(*) FROM stats WHERE action='fail'")
        await msg.answer(
            f"📊 Всего токенов: {total}\n"
            f"🟢 Живых: {alive}\n"
            f"🔴 Мёртвых: {total - alive}\n\n"
            f"✅ Авторизовано: {success or 0}\n"
            f"❌ Ошибок: {fail or 0}\n\n"
            f"/add — загрузить токены"
        )

@dp.message(Command("add"))
async def add_cmd(msg: Message):
    if msg.chat.type != "private": return
    await msg.answer("📥 Отправь токены. Каждый с новой строки.")

@dp.message(Command("setup"))
async def setup_cmd(msg: Message):
    if msg.chat.type not in ("group","supergroup"): return
    try:
        await add_group(int(msg.text.split()[1]))
        await msg.answer("✅ Группа одобрена")
    except:
        await msg.answer("❌ /setup ID_группы")

@dp.message(Command("stats"))
async def stats_cmd(msg: Message):
    if msg.chat.type not in ("group","supergroup"): return
    if not await is_group_approved(msg.chat.id): return

    rows = await get_today_stats()
    msk = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M")

    users = {}
    for r in rows:
        u = r["username"] or "неизвестен"
        if u not in users: users[u] = {"success": 0, "fail": 0}
        if r["action"] == "success": users[u]["success"] += 1
        else: users[u]["fail"] += 1

    text = f"📊 Стата за сегодня ({msk} МСК)\n\n"
    text += "┌──────────────────────────────┐\n"
    text += "│ User          ✅   ❌   🔄  │\n"
    text += "├──────────────────────────────┤\n"
    for u, d in users.items():
        name = u[:12]
        text += f"│ {name:<12}  {d['success']:<3}  {d['fail']:<3}  {d['success']+d['fail']:<3} │\n"
    text += "└──────────────────────────────┘"
    await msg.answer(f"<pre>{text}</pre>", parse_mode="HTML")

@dp.message()
async def handler(msg: Message):
    if msg.chat.type == "private" and msg.text:
        lines = [l.strip() for l in msg.text.split("\n") if l.strip()]
        tokens = [extract_token(l) for l in lines if extract_token(l)]
        if not tokens: return
        n = 0
        for t in tokens:
            await save(t)
            n += 1
        total, alive = await get_counts()
        await msg.answer(f"✅ {n} токенов. Всего: {total}, живых: {alive}")
        return

    if msg.chat.type in ("group", "supergroup") and msg.photo:
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
                await log_stat(msg.from_user.id, msg.from_user.username or msg.from_user.full_name, "fail")
                await msg.reply("Не удалось распознать QR-код.")
                return

            if "max.ru" not in qr_data and "oneme.ru" not in qr_data:
                await log_stat(msg.from_user.id, msg.from_user.username or msg.from_user.full_name, "fail")
                await msg.reply("Это не QR-код от MAX.")
                return

            tried = 0
            while True:
                t = await get_token()
                if not t:
                    await msg.reply("Нет доступных токенов.")
                    return

                tried += 1
                try:
                    c = Client(phone="+79990000000", work_dir="cache", session_name=f"qr_{t['id']}.db", extra_config=ExtraConfig(token=t['token']))
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
                    await log_stat(msg.from_user.id, msg.from_user.username or msg.from_user.full_name, "success")
                    await msg.reply(f"Номер {phone} успешно авторизован.")
                    return

                except Exception as e:
                    err = str(e).lower()
                    if "expired" in err:
                        await log_stat(msg.from_user.id, msg.from_user.username or msg.from_user.full_name, "fail")
                        await msg.reply("QR-код устарел.")
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
            await msg.reply("Ошибка обработки.")
        finally:
            processing.discard(msg.message_id)

async def main():
    await init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
