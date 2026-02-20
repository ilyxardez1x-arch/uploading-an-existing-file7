import asyncio
import aiosqlite
import random
import string
import os
from datetime import date
from collections import defaultdict
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from groq import Groq
import base64
import httpx

# ========== НАСТРОЙКИ ==========
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
OWNER_ID       = 6210516253
# ================================

FREE_TEXT    = 10
FREE_IMAGES  = 2
PREM_TEXT    = 90
PREM_IMAGES  = 15
MAX_HISTORY  = 20
DB_PATH      = "bot.db"

client = Groq(api_key=GROQ_API_KEY)
bot    = Bot(token=TELEGRAM_TOKEN)
dp     = Dispatcher()

chat_history: dict[int, list] = defaultdict(list)

# ──────────────────────────────────────────
# БД
# ──────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                premium     INTEGER DEFAULT 0,
                is_admin    INTEGER DEFAULT 0,
                text_today  INTEGER DEFAULT 0,
                img_today   INTEGER DEFAULT 0,
                last_date   TEXT DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promos (
                code     TEXT PRIMARY KEY,
                used_by  INTEGER DEFAULT NULL
            )
        """)
        await db.commit()

async def get_user(user_id: int) -> dict:
    today = str(date.today())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        row = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
        if not row:
            is_admin = 1 if user_id == OWNER_ID else 0
            await db.execute(
                "INSERT INTO users (user_id, last_date, is_admin) VALUES (?,?,?)",
                (user_id, today, is_admin)
            )
            await db.commit()
            return await get_user(user_id)
        if row["last_date"] != today:
            await db.execute(
                "UPDATE users SET text_today=0, img_today=0, last_date=? WHERE user_id=?",
                (today, user_id)
            )
            await db.commit()
            return await get_user(user_id)
        return dict(row)

async def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    u = await get_user(user_id)
    return bool(u.get("is_admin", 0))

async def set_admin(user_id: int, val: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("UPDATE users SET is_admin=? WHERE user_id=?", (val, user_id))
        await db.commit()

async def inc_counter(user_id: int, kind: str):
    col = "text_today" if kind == "text" else "img_today"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(f"UPDATE users SET {col}={col}+1 WHERE user_id=?", (user_id,))
        await db.commit()

async def set_premium(user_id: int, val: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("UPDATE users SET premium=? WHERE user_id=?", (val, user_id))
        await db.commit()

async def add_promo(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("INSERT OR IGNORE INTO promos VALUES (?,NULL)", (code,))
        await db.commit()

async def use_promo(code: str, user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        row = await (await db.execute("SELECT * FROM promos WHERE code=?", (code,))).fetchone()
        if not row:        return "not_found"
        if row["used_by"]: return "used"
        await db.execute("UPDATE promos SET used_by=? WHERE code=?", (user_id, code))
        await db.execute("UPDATE users SET premium=1 WHERE user_id=?", (user_id,))
        await db.commit()
        return "ok"

async def list_promos():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM promos")).fetchall()
        return [dict(r) for r in rows]

async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        total   = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        premium = (await (await db.execute("SELECT COUNT(*) FROM users WHERE premium=1")).fetchone())[0]
        admins  = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_admin=1")).fetchone())[0]
        promos  = (await (await db.execute("SELECT COUNT(*) FROM promos")).fetchone())[0]
        used    = (await (await db.execute("SELECT COUNT(*) FROM promos WHERE used_by IS NOT NULL")).fetchone())[0]
        return total, premium, admins, promos, used

async def get_admins():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT user_id FROM users WHERE is_admin=1")).fetchall()
        return [r["user_id"] for r in rows]

def gen_code(length=10):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def check_limit(user: dict, kind: str):
    limit = (PREM_TEXT if kind == "text" else PREM_IMAGES) if user["premium"] else (FREE_TEXT if kind == "text" else FREE_IMAGES)
    used  = user["text_today"] if kind == "text" else user["img_today"]
    return used < limit, used, limit

async def download_image(file_id: str) -> str:
    file = await bot.get_file(file_id)
    url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
    async with httpx.AsyncClient() as c:
        r = await c.get(url)
        return base64.b64encode(r.content).decode()

# ──────────────────────────────────────────
# Клавиатуры
# ──────────────────────────────────────────
async def kb_main(user_id: int):
    rows = [
        [
            InlineKeyboardButton(text="👤 Профиль",     callback_data="profile"),
            InlineKeyboardButton(text="🔄 Сброс чата",  callback_data="reset"),
        ],
        [
            InlineKeyboardButton(text="🎁 Промокод",    callback_data="promo"),
            InlineKeyboardButton(text="ℹ️ Помощь",       callback_data="help"),
        ],
    ]
    if await is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_admin(is_owner: bool):
    rows = [
        [
            InlineKeyboardButton(text="📊 Статистика",     callback_data="adm_stats"),
            InlineKeyboardButton(text="🎁 Создать промо",  callback_data="adm_genpromo"),
        ],
        [
            InlineKeyboardButton(text="📋 Список промо",   callback_data="adm_listpromos"),
        ],
        [
            InlineKeyboardButton(text="⭐ Выдать премиум", callback_data="adm_setprem"),
            InlineKeyboardButton(text="🚫 Снять премиум",  callback_data="adm_remprem"),
        ],
    ]
    if is_owner:
        rows.append([
            InlineKeyboardButton(text="👑 Выдать админку", callback_data="adm_setadmin"),
            InlineKeyboardButton(text="❌ Снять админку",  callback_data="adm_remadmin"),
        ])
        rows.append([InlineKeyboardButton(text="📋 Список админов", callback_data="adm_listadmins")])
    rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

def kb_back_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В админку", callback_data="admin")]
    ])

def profile_text(u: dict) -> str:
    status      = "⭐ Премиум" if u["premium"] else "🆓 Бесплатно"
    admin_badge = "👑 Админ\n" if u.get("is_admin") else ""
    tl = PREM_TEXT   if u["premium"] else FREE_TEXT
    il = PREM_IMAGES if u["premium"] else FREE_IMAGES
    used_t = u["text_today"]
    used_i = u["img_today"]
    bar_t  = "▓" * used_t + "░" * (tl - used_t) if tl <= 20 else f"{used_t}/{tl}"
    bar_i  = "▓" * used_i + "░" * (il - used_i)  if il  <= 20 else f"{used_i}/{il}"
    return (
        f"╔══════════════════╗\n"
        f"║    👤  Профиль    ║\n"
        f"╚══════════════════╝\n\n"
        f"🏷 Статус: {status}\n"
        f"{admin_badge}\n"
        f"📝 Запросы сегодня:\n"
        f"  {bar_t}  ({used_t}/{tl})\n\n"
        f"🖼 Фото сегодня:\n"
        f"  {bar_i}  ({used_i}/{il})\n\n"
        f"🔄 Лимиты обновляются каждый день"
    )

# ──────────────────────────────────────────
# Состояния ожидания
# ──────────────────────────────────────────
waiting_promo:    set[int] = set()
waiting_setprem:  set[int] = set()
waiting_remprem:  set[int] = set()
waiting_genpromo: set[int] = set()
waiting_setadmin: set[int] = set()
waiting_remadmin: set[int] = set()

# ──────────────────────────────────────────
# /start
# ──────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await get_user(msg.from_user.id)
    chat_history[msg.from_user.id].clear()
    name = msg.from_user.first_name or "друг"
    await msg.answer(
        f"👋 Привет, {name}!\n\n"
        f"Я AI-бот на базе <b>Llama 3</b> 🤖\n\n"
        f"💬 Просто напиши мне что-нибудь\n"
        f"🖼 Или отправь фото — я его опишу\n\n"
        f"Используй меню ниже 👇",
        reply_markup=await kb_main(msg.from_user.id),
        parse_mode="HTML"
    )

# ──────────────────────────────────────────
# Callbacks
# ──────────────────────────────────────────
@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery):
    name = cb.from_user.first_name or "друг"
    await cb.message.edit_text(
        f"👋 Привет, {name}!\n\n"
        f"Я AI-бот на базе <b>Llama 3</b> 🤖\n\n"
        f"💬 Просто напиши мне что-нибудь\n"
        f"🖼 Или отправь фото — я его опишу\n\n"
        f"Используй меню ниже 👇",
        reply_markup=await kb_main(cb.from_user.id),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    await cb.message.edit_text(profile_text(u), reply_markup=kb_back())

@dp.callback_query(F.data == "reset")
async def cb_reset(cb: CallbackQuery):
    chat_history[cb.from_user.id].clear()
    await cb.message.edit_text(
        "🔄 <b>История чата очищена!</b>\n\nМожешь начинать новый разговор.",
        reply_markup=kb_back(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await cb.message.edit_text(
        "╔══════════════════╗\n"
        "║    ℹ️  Помощь     ║\n"
        "╚══════════════════╝\n\n"
        "💬 <b>Чат</b> — просто пиши сообщения\n"
        "🖼 <b>Фото</b> — отправь картинку (можно с вопросом)\n"
        "🔄 <b>Сброс чата</b> — очистить историю разговора\n"
        "🎁 <b>Промокод</b> — активировать премиум\n\n"
        "🆓 <b>Бесплатно:</b> 10 запросов + 2 фото в день\n"
        "⭐ <b>Премиум:</b> 90 запросов + 15 фото в день",
        reply_markup=kb_back(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "promo")
async def cb_promo(cb: CallbackQuery):
    waiting_promo.add(cb.from_user.id)
    await cb.message.edit_text(
        "🎁 <b>Введи промокод</b>\n\nОтправь его следующим сообщением:",
        reply_markup=kb_back(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin")
async def cb_admin(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    owner = cb.from_user.id == OWNER_ID
    await cb.message.edit_text(
        "╔══════════════════╗\n"
        "║  🛠  Админ-панель ║\n"
        "╚══════════════════╝\n\n"
        "Выбери действие:",
        reply_markup=kb_admin(owner)
    )

@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    total, premium, admins, promos, used = await get_stats()
    await cb.message.edit_text(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"⭐ Премиум: <b>{premium}</b>\n"
        f"🆓 Бесплатных: <b>{total - premium}</b>\n"
        f"👑 Админов: <b>{admins}</b>\n\n"
        f"🎁 Промокодов создано: <b>{promos}</b>\n"
        f"✅ Использовано: <b>{used}</b>\n"
        f"🟢 Свободно: <b>{promos - used}</b>",
        reply_markup=kb_back_admin(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_genpromo")
async def cb_adm_genpromo(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    waiting_genpromo.add(cb.from_user.id)
    await cb.message.edit_text(
        "🎁 <b>Создание промокодов</b>\n\nСколько промокодов создать?\nОтправь число (например: 5):",
        reply_markup=kb_back_admin(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_listpromos")
async def cb_adm_listpromos(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    promos = await list_promos()
    if not promos:
        await cb.message.edit_text("Промокодов нет", reply_markup=kb_back_admin())
        return
    lines = []
    for p in promos:
        status = f"✅ (id: {p['used_by']})" if p["used_by"] else "🟢 свободен"
        lines.append(f"<code>{p['code']}</code> — {status}")
    text = "📋 <b>Все промокоды:</b>\n\n" + "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await cb.message.edit_text(text, reply_markup=kb_back_admin(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_setprem")
async def cb_adm_setprem(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    waiting_setprem.add(cb.from_user.id)
    await cb.message.edit_text(
        "⭐ <b>Выдать премиум</b>\n\nОтправь Telegram ID пользователя:",
        reply_markup=kb_back_admin(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_remprem")
async def cb_adm_remprem(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    waiting_remprem.add(cb.from_user.id)
    await cb.message.edit_text(
        "🚫 <b>Снять премиум</b>\n\nОтправь Telegram ID пользователя:",
        reply_markup=kb_back_admin(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_setadmin")
async def cb_adm_setadmin(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("❌ Только владелец", show_alert=True)
        return
    waiting_setadmin.add(cb.from_user.id)
    await cb.message.edit_text(
        "👑 <b>Выдать админку</b>\n\nОтправь Telegram ID пользователя:",
        reply_markup=kb_back_admin(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_remadmin")
async def cb_adm_remadmin(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("❌ Только владелец", show_alert=True)
        return
    waiting_remadmin.add(cb.from_user.id)
    await cb.message.edit_text(
        "❌ <b>Снять админку</b>\n\nОтправь Telegram ID пользователя:",
        reply_markup=kb_back_admin(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_listadmins")
async def cb_adm_listadmins(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID: return
    admins = await get_admins()
    lines  = [f"• <code>{a}</code>{'  👑 ты' if a == OWNER_ID else ''}" for a in admins]
    await cb.message.edit_text(
        "👑 <b>Список админов:</b>\n\n" + "\n".join(lines),
        reply_markup=kb_back_admin(), parse_mode="HTML"
    )

# ──────────────────────────────────────────
# Фото
# ──────────────────────────────────────────
@dp.message(F.photo)
async def handle_photo(msg: Message):
    u = await get_user(msg.from_user.id)
    ok, used, limit = check_limit(u, "image")
    if not ok:
        kb = None if u["premium"] else InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="promo")
        ]])
        await msg.answer(f"❌ <b>Лимит фото исчерпан</b> ({used}/{limit} сегодня)", reply_markup=kb, parse_mode="HTML")
        return
    await bot.send_chat_action(msg.chat.id, "typing")
    try:
        photo   = msg.photo[-1]
        img_b64 = await download_image(photo.file_id)
        caption = msg.caption or "Что изображено на фото? Опиши подробно."
        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": [
                {"type": "text",      "text": caption},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            ]}]
        )
        await inc_counter(msg.from_user.id, "image")
        answer = response.choices[0].message.content
        chat_history[msg.from_user.id].append({"role": "user",      "content": f"[Фото] {caption}"})
        chat_history[msg.from_user.id].append({"role": "assistant", "content": answer})
        await msg.answer(answer)
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {str(e)}")

# ──────────────────────────────────────────
# Текст
# ──────────────────────────────────────────
@dp.message()
async def handle_text(msg: Message):
    if not msg.text:
        return
    uid = msg.from_user.id

    if uid in waiting_promo:
        waiting_promo.discard(uid)
        result = await use_promo(msg.text.strip().upper(), uid)
        if result == "not_found":
            await msg.answer("❌ Такого промокода не существует", reply_markup=kb_back())
        elif result == "used":
            await msg.answer("❌ Промокод уже использован", reply_markup=kb_back())
        else:
            await msg.answer("✅ <b>Промокод активирован!</b>\n\nТеперь у тебя ⭐ <b>Премиум</b>\n90 запросов + 15 фото в день 🎉", reply_markup=kb_back(), parse_mode="HTML")
        return

    if uid in waiting_genpromo and await is_admin(uid):
        waiting_genpromo.discard(uid)
        count = min(int(msg.text.strip()) if msg.text.strip().isdigit() else 1, 50)
        codes = []
        for _ in range(count):
            code = gen_code()
            await add_promo(code)
            codes.append(code)
        text = f"🎁 <b>Создано {count} промокодов:</b>\n\n" + "\n".join(f"• <code>{c}</code>" for c in codes)
        await msg.answer(text, reply_markup=kb_back_admin(), parse_mode="HTML")
        return

    if uid in waiting_setprem and await is_admin(uid):
        waiting_setprem.discard(uid)
        if msg.text.strip().isdigit():
            target = int(msg.text.strip())
            await get_user(target)
            await set_premium(target, 1)
            await msg.answer(f"✅ Пользователю <code>{target}</code> выдан ⭐ Премиум", reply_markup=kb_back_admin(), parse_mode="HTML")
        else:
            await msg.answer("❌ Неверный ID", reply_markup=kb_back_admin())
        return

    if uid in waiting_remprem and await is_admin(uid):
        waiting_remprem.discard(uid)
        if msg.text.strip().isdigit():
            target = int(msg.text.strip())
            await set_premium(target, 0)
            await msg.answer(f"✅ У пользователя <code>{target}</code> снят премиум", reply_markup=kb_back_admin(), parse_mode="HTML")
        else:
            await msg.answer("❌ Неверный ID", reply_markup=kb_back_admin())
        return

    if uid in waiting_setadmin and uid == OWNER_ID:
        waiting_setadmin.discard(uid)
        if msg.text.strip().isdigit():
            target = int(msg.text.strip())
            if target == OWNER_ID:
                await msg.answer("👑 Это уже ты!", reply_markup=kb_back_admin())
                return
            await get_user(target)
            await set_admin(target, 1)
            await msg.answer(f"✅ Пользователю <code>{target}</code> выдана 👑 Админка", reply_markup=kb_back_admin(), parse_mode="HTML")
            try:
                await bot.send_message(target, "🎉 Тебе выдали права <b>Администратора</b>!\n\nНажми /start чтобы увидеть панель.", parse_mode="HTML")
            except: pass
        else:
            await msg.answer("❌ Неверный ID", reply_markup=kb_back_admin())
        return

    if uid in waiting_remadmin and uid == OWNER_ID:
        waiting_remadmin.discard(uid)
        if msg.text.strip().isdigit():
            target = int(msg.text.strip())
            if target == OWNER_ID:
                await msg.answer("❌ Нельзя снять самого себя!", reply_markup=kb_back_admin())
                return
            await set_admin(target, 0)
            await msg.answer(f"✅ У <code>{target}</code> снята админка", reply_markup=kb_back_admin(), parse_mode="HTML")
        else:
            await msg.answer("❌ Неверный ID", reply_markup=kb_back_admin())
        return

    # Обычный чат
    u = await get_user(uid)
    ok, used, limit = check_limit(u, "text")
    if not ok:
        kb = None if u["premium"] else InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="promo")
        ]])
        await msg.answer(f"❌ <b>Лимит исчерпан</b> ({used}/{limit} сегодня)\n\nЛимиты обновятся завтра.", reply_markup=kb, parse_mode="HTML")
        return

    await bot.send_chat_action(msg.chat.id, "typing")
    try:
        history = chat_history[uid]
        history.append({"role": "user", "content": msg.text})
        if len(history) > MAX_HISTORY:
            chat_history[uid] = history[-MAX_HISTORY:]
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=chat_history[uid]
        )
        answer = response.choices[0].message.content
        chat_history[uid].append({"role": "assistant", "content": answer})
        await inc_counter(uid, "text")
        await msg.answer(answer)
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {str(e)}")

# ──────────────────────────────────────────
async def main():
    await init_db()
    print("🤖 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
