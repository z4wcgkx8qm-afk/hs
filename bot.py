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
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymax import Client, ExtraConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = {7742243877, 8706712229}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None
processing = set()
expecting_tokens = set()

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
        await c.execute("CREATE TABLE IF NOT EXISTS stats (id SERIAL PRIMARY KEY, user_id BIGINT, username TEXT, action TEXT, created_at TIMESTAMP DEFAULT NOW())")

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
        success = await c.fetchval("SELECT COUNT(*) FROM stats WHERE action='success'")
        today = await c.fetchval("SELECT COUNT(*) FROM stats WHERE action='success' AND created_at::date=CURRENT_DATE")
        return total, alive, success or 0, today or 0

async def is_group_approved(gid):
    async with db_pool.acquire() as c:
        return await c.fetchval("SELECT 1 FROM approved_groups WHERE group_id=$1", gid)

async def add_group(gid):
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO approved_groups VALUES($1) ON CONFLICT DO NOTHING", gid)

async def remove_group(gid):
    async with db_pool.acquire() as c:
        await c.execute("DELETE FROM approved_groups WHERE group_id=$1", gid)

async def get_today_stats():
    async with db_pool.acquire() as c:
        return await c.fetch("SELECT username, action FROM stats WHERE created_at::date=CURRENT_DATE")

@dp.message(Command("start"))
async def start(msg: Message):
    if msg.chat.type != "private" or msg.from_user.id not in ADMIN_IDS:
        return

    total, alive, success, today = await get_counts()
    dead = total - alive

    text = (
        f"🔐 <b>Faung Scan</b>\n\n"
        f"В наличии токенов: <code>{total}</code>\n"
        f"Авторизовано всего: <code>{success}</code>\n"
        f"Авторизовано за сегодня: <code>{today}</code>\n"
        f"Мёртвых токенов: <code>{dead}</code>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить токены", callback_data="add_tokens")]
    ])

    await msg.answer(text, reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(lambda c: c.data == "add_tokens")
async def add_tokens_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Доступ запрещён", show_alert=True)

    expecting_tokens.add(callback.from_user.id)
    await callback.message.answer(
        "🔖 Отправьте мне токены ANDROID в любом формате.\n\n"
        "<i>Чтобы закончить действие, введите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.message(Command("cancel"))
async def cancel(msg: Message):
    if msg.from_user.id in ADMIN_IDS and msg.chat.type == "private":
        expecting_tokens.discard(msg.from_user.id)
        await msg.answer("❌ Загрузка токенов отменена.")

@dp.message(Command("setup"))
async def setup_cmd(msg: Message):
    if msg.from_user.id not in ADMIN_IDS: return
    try:
        await add_group(int(msg.text.split()[1]))
        await msg.answer("✅ Группа одобрена")
    except:
        await msg.answer("❌ /setup ID_группы")

@dp.message(Command("unsetup"))
async def unsetup_cmd(msg: Message):
    if msg.from_user.id not in ADMIN_IDS: return
    try:
        await remove_group(int(msg.text.split()[1]))
        await msg.answer("✅ Группа удалена из одобренных")
    except:
        await msg.answer("❌ /unsetup ID_группы")

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
    if msg.chat.type == "private" and msg.from_user.id in ADMIN_IDS and msg.from_user.id in expecting_tokens:
        if msg.text:
            lines = [l.strip() for l in msg.text.split("\n") if l.strip()]
            tokens = [extract_token(l) for l in lines if extract_token(l)]
            if not tokens:
                await msg.answer("❌ Неверный формат токенов. Отправьте токены, каждый с новой строки")
                return
            n = 0
            for t in tokens:
                await save(t)
                n += 1
            expecting_tokens.discard(msg.from_user.id)
            await msg.answer(
                f"✅ Токены <code>{n}</code> добавлены в базу данных.\n\n"
                f"<i>Можете начать авторизацию через одобренную группу</i>",
                parse_mode="HTML"
            )
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
                await msg.reply("❌ QR-код не распознан. Убедитесь, что изображение чёткое и содержит QR с web.max.ru")
                return

            if "max.ru" not in qr_data and "oneme.ru" not in qr_data:
                await log_stat(msg.from_user.id, msg.from_user.username or msg.from_user.full_name, "fail")
                await msg.reply("❌ Это не QR-код от MAX. Отправьте скриншот с web.max.ru")
                return

            tried = 0
            while True:
                t = await get_token()
                if not t:
                    await msg.reply("❌ Нет доступных токенов. Загрузите токены через /add в личные сообщения")
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
                    await msg.reply(f"✅ Номер {phone} успешно авторизован. Токен обработан.")
                    return

                except Exception as e:
                    err = str(e).lower()
                    if "expired" in err:
                        await log_stat(msg.from_user.id, msg.from_user.username or msg.from_user.full_name, "fail")
                        await msg.reply("❌ QR-код устарел. Сделайте новый скриншот и попробуйте снова")
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
            await msg.reply("❌ Произошла ошибка. Попробуйте позже")
        finally:
            processing.discard(msg.message_id)

async def main():
    await init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
