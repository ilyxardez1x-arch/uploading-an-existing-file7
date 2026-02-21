import asyncio
import logging
import random
import string
import sqlite3
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))

# ═══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════
conn = sqlite3.connect("chat.db", check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id       INTEGER PRIMARY KEY,
        anon_name     TEXT NOT NULL,
        chats_count   INTEGER DEFAULT 0,
        messages_sent INTEGER DEFAULT 0,
        is_banned     INTEGER DEFAULT 0,
        referred_by   INTEGER DEFAULT NULL,
        ref_count     INTEGER DEFAULT 0,
        rating_sum    INTEGER DEFAULT 0,
        rating_count  INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS chats (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        user1_id  INTEGER,
        user2_id  INTEGER,
        ended     INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS messages (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id   INTEGER,
        sender_id INTEGER,
        nick      TEXT,
        content   TEXT,
        ts        TEXT DEFAULT (strftime('%H:%M', 'now'))
    );
    CREATE TABLE IF NOT EXISTS queue (
        user_id INTEGER PRIMARY KEY
    );
    CREATE TABLE IF NOT EXISTS reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id INTEGER,
        reported_id INTEGER,
        chat_id     INTEGER,
        status      TEXT DEFAULT 'pending'
    );
    CREATE TABLE IF NOT EXISTS ratings (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        rater_id INTEGER,
        rated_id INTEGER,
        chat_id  INTEGER,
        score    INTEGER
    );
""")
conn.commit()

# ═══════════════════════════════════════════════════════════════
#  ХЕЛПЕРЫ
# ═══════════════════════════════════════════════════════════════

def rnd_name():
    adj  = ["Тихий","Быстрый","Мудрый","Смелый","Хитрый","Добрый","Тёмный","Яркий","Дерзкий","Ленивый"]
    noun = ["Лис","Волк","Орёл","Тигр","Медведь","Сова","Рысь","Кот","Дракон","Заяц"]
    return f"{random.choice(adj)}{random.choice(noun)}{''.join(random.choices(string.digits,k=3))}"

def get_user(uid):
    return conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def is_banned(uid):
    u = get_user(uid)
    return bool(u and u["is_banned"])

def get_partner(uid):
    row = conn.execute(
        "SELECT CASE WHEN user1_id=? THEN user2_id ELSE user1_id END as p "
        "FROM chats WHERE (user1_id=? OR user2_id=?) AND ended=0 LIMIT 1",
        (uid, uid, uid)
    ).fetchone()
    return row["p"] if row else None

def get_active_chat_id(uid):
    row = conn.execute(
        "SELECT id FROM chats WHERE (user1_id=? OR user2_id=?) AND ended=0 LIMIT 1",
        (uid, uid)
    ).fetchone()
    return row["id"] if row else None

def in_queue(uid):
    return conn.execute("SELECT 1 FROM queue WHERE user_id=?", (uid,)).fetchone() is not None

def save_msg(chat_id, sender_id, nick, content):
    conn.execute(
        "INSERT INTO messages (chat_id,sender_id,nick,content) VALUES (?,?,?,?)",
        (chat_id, sender_id, nick, content)
    )
    conn.commit()

def format_dialog(chat_id):
    rows = conn.execute(
        "SELECT nick, content, ts FROM messages WHERE chat_id=? ORDER BY id",
        (chat_id,)
    ).fetchall()
    if not rows:
        return "_(диалог пуст)_"
    return "\n".join(f"[{r['ts']}] *{r['nick']}*: {r['content']}" for r in rows)

def avg_rating(uid):
    u = get_user(uid)
    if not u or u["rating_count"] == 0:
        return "нет оценок"
    return f"{u['rating_sum']/u['rating_count']:.1f} ⭐ ({u['rating_count']} оценок)"

# ═══════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════

MENU_MAIN = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🔍 Найти чат"),   KeyboardButton(text="👤 Профиль")],
    [KeyboardButton(text="🔗 Реферальная"), KeyboardButton(text="📊 Статистика")],
], resize_keyboard=True, persistent=True)

MENU_CHAT = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🚪 Покинуть чат")],
], resize_keyboard=True, persistent=True)

def rating_kb(partner_id, chat_id):
    def btn(s, i):
        return InlineKeyboardButton(text=s, callback_data=f"rate_{partner_id}_{chat_id}_{i}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("⭐ 1", 1), btn("⭐⭐ 2", 2), btn("⭐⭐⭐ 3", 3)],
        [btn("⭐⭐⭐⭐ 4", 4), btn("⭐⭐⭐⭐⭐ 5", 5)],
        [InlineKeyboardButton(text="🚨 Пожаловаться", callback_data=f"report_{partner_id}_{chat_id}")],
        [InlineKeyboardButton(text="✖️ Пропустить",   callback_data="skip_rating")],
    ])

def admin_kb(report_id, reported_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔨 Забанить",         callback_data=f"adm_ban_{report_id}_{reported_id}"),
        InlineKeyboardButton(text="✅ Пропустить",        callback_data=f"adm_skip_{report_id}"),
        InlineKeyboardButton(text="🔒 Закрыть проверку", callback_data=f"adm_close_{report_id}"),
    ]])

# ═══════════════════════════════════════════════════════════════
#  РОУТЕР
# ═══════════════════════════════════════════════════════════════
router = Router()

# ── /start ──────────────────────────────────────────────────────
@router.message(CommandStart())
async def start(message: Message, bot: Bot):
    uid  = message.from_user.id
    args = message.text.split()[1] if len(message.text.split()) > 1 else ""

    if is_banned(uid):
        await message.answer("🚫 Вы заблокированы.")
        return

    user = get_user(uid)
    if not user:
        name   = rnd_name()
        ref_by = None
        if args.startswith("ref_"):
            try:
                ref_by = int(args[4:])
                if ref_by == uid:
                    ref_by = None
            except ValueError:
                pass

        conn.execute("INSERT INTO users (user_id,anon_name,referred_by) VALUES (?,?,?)", (uid, name, ref_by))
        conn.commit()

        if ref_by and get_user(ref_by):
            conn.execute("UPDATE users SET ref_count=ref_count+1 WHERE user_id=?", (ref_by,))
            conn.commit()
            ru = get_user(ref_by)
            await bot.send_message(ref_by, f"🎉 По вашей ссылке зарегистрировался новый пользователь!\n👥 Рефералов: *{ru['ref_count']}*", parse_mode="Markdown")

        text = f"👋 Добро пожаловать в *Анонимный Чат*!\n\nВаше имя: *{name}*\n\nКнопки внизу 👇"
    else:
        text = f"👋 С возвращением, *{user['anon_name']}*!\nКнопки внизу 👇"

    await message.answer(text, parse_mode="Markdown", reply_markup=MENU_MAIN)

# ── Текстовые сообщения (кнопки меню + чат) ────────────────────
@router.message(F.text)
async def handle_text(message: Message, bot: Bot):
    uid  = message.from_user.id
    text = message.text

    if is_banned(uid):
        await message.answer("🚫 Вы заблокированы.")
        return

    if text == "🔍 Найти чат":
        await do_find(uid, message, bot)
    elif text == "🚪 Покинуть чат":
        await do_leave(uid, message, bot)
    elif text == "👤 Профиль":
        await show_profile(uid, message)
    elif text == "📊 Статистика":
        await show_stats(message)
    elif text == "🔗 Реферальная":
        await show_ref(uid, message, bot)
    else:
        pid = get_partner(uid)
        if not pid:
            if in_queue(uid):
                await message.answer("🔍 Ещё ищем собеседника…")
            else:
                await message.answer("❗ Вы не в чате. Нажмите «🔍 Найти чат».", reply_markup=MENU_MAIN)
            return
        await relay(message, bot, uid, pid)

# ── Медиа ───────────────────────────────────────────────────────
@router.message(F.photo | F.video | F.voice | F.sticker | F.animation | F.document | F.video_note | F.audio)
async def handle_media(message: Message, bot: Bot):
    uid = message.from_user.id
    if is_banned(uid):
        return
    pid = get_partner(uid)
    if not pid:
        if in_queue(uid):
            await message.answer("🔍 Ещё ищем…")
        else:
            await message.answer("❗ Вы не в чате.", reply_markup=MENU_MAIN)
        return
    await relay(message, bot, uid, pid)

# ═══════════════════════════════════════════════════════════════
#  ЛОГИКА ЧАТА
# ═══════════════════════════════════════════════════════════════

async def do_find(uid, message: Message, bot: Bot):
    if get_partner(uid):
        await message.answer("❗ Вы уже в чате.", reply_markup=MENU_CHAT)
        return
    if in_queue(uid):
        await message.answer("🔍 Уже ищем, подождите…")
        return

    waiting = conn.execute("SELECT user_id FROM queue WHERE user_id!=? LIMIT 1", (uid,)).fetchone()
    if waiting:
        pid = waiting["user_id"]
        conn.execute("DELETE FROM queue WHERE user_id=?", (pid,))
        conn.execute("INSERT INTO chats (user1_id,user2_id) VALUES (?,?)", (uid, pid))
        conn.execute("UPDATE users SET chats_count=chats_count+1 WHERE user_id IN (?,?)", (uid, pid))
        conn.commit()
        u1, u2 = get_user(uid), get_user(pid)
        await message.answer(f"✅ Собеседник найден!\nПартнёр: *{u2['anon_name']}*\n\n💬 Пишите!", parse_mode="Markdown", reply_markup=MENU_CHAT)
        await bot.send_message(pid, f"✅ Собеседник найден!\nПартнёр: *{u1['anon_name']}*\n\n💬 Пишите!", parse_mode="Markdown", reply_markup=MENU_CHAT)
    else:
        conn.execute("INSERT OR IGNORE INTO queue (user_id) VALUES (?)", (uid,))
        conn.commit()
        await message.answer("🔍 Ищем собеседника…\nКак только кто-то появится — чат начнётся!", reply_markup=MENU_MAIN)

async def do_leave(uid, message: Message, bot: Bot):
    if in_queue(uid):
        conn.execute("DELETE FROM queue WHERE user_id=?", (uid,))
        conn.commit()
        await message.answer("✅ Поиск отменён.", reply_markup=MENU_MAIN)
        return

    pid = get_partner(uid)
    if not pid:
        await message.answer("❗ Вы не в чате.", reply_markup=MENU_MAIN)
        return

    user    = get_user(uid)
    chat_id = get_active_chat_id(uid)
    conn.execute("UPDATE chats SET ended=1 WHERE id=?", (chat_id,))
    conn.commit()

    await message.answer("👋 Чат завершён!", reply_markup=MENU_MAIN)
    await message.answer("⭐ Оцените собеседника:", reply_markup=rating_kb(pid, chat_id))

    await bot.send_message(pid, f"👋 Собеседник *{user['anon_name']}* покинул чат.", parse_mode="Markdown", reply_markup=MENU_MAIN)
    await bot.send_message(pid, "⭐ Оцените собеседника:", reply_markup=rating_kb(uid, chat_id))

async def relay(message: Message, bot: Bot, uid, pid):
    user    = get_user(uid)
    chat_id = get_active_chat_id(uid)
    conn.execute("UPDATE users SET messages_sent=messages_sent+1 WHERE user_id=?", (uid,))
    conn.commit()

    label = None
    try:
        if message.text:
            await bot.send_message(pid, f"💬 {message.text}")
            label = message.text
        elif message.photo:
            await bot.send_photo(pid, message.photo[-1].file_id, caption=message.caption or "")
            label = f"[📷 Фото]{' | '+message.caption if message.caption else ''}"
        elif message.video:
            await bot.send_video(pid, message.video.file_id, caption=message.caption or "")
            label = "[🎥 Видео]"
        elif message.voice:
            await bot.send_voice(pid, message.voice.file_id)
            label = "[🎤 Голосовое]"
        elif message.sticker:
            await bot.send_sticker(pid, message.sticker.file_id)
            label = f"[🎭 Стикер {message.sticker.emoji or ''}]"
        elif message.animation:
            await bot.send_animation(pid, message.animation.file_id)
            label = "[GIF]"
        elif message.document:
            await bot.send_document(pid, message.document.file_id, caption=message.caption or "")
            label = f"[📎 {message.document.file_name}]"
        elif message.video_note:
            await bot.send_video_note(pid, message.video_note.file_id)
            label = "[⭕ Видеосообщение]"
        elif message.audio:
            await bot.send_audio(pid, message.audio.file_id)
            label = "[🎵 Аудио]"
    except Exception as e:
        logger.error(f"Relay error: {e}")

    if chat_id and label:
        save_msg(chat_id, uid, user["anon_name"], label)

# ═══════════════════════════════════════════════════════════════
#  ПРОФИЛЬ / СТАТИСТИКА / РЕФЕРАЛЬНАЯ
# ═══════════════════════════════════════════════════════════════

async def show_profile(uid, message: Message):
    u  = get_user(uid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Сменить имя", callback_data="newname")
    ]])
    await message.answer(
        f"👤 *Ваш профиль*\n\n"
        f"🎭 Имя: *{u['anon_name']}*\n"
        f"💬 Чатов: *{u['chats_count']}*\n"
        f"✉️ Сообщений: *{u['messages_sent']}*\n"
        f"⭐ Рейтинг: *{avg_rating(uid)}*\n"
        f"👥 Рефералов: *{u['ref_count']}*",
        parse_mode="Markdown", reply_markup=kb
    )

async def show_stats(message: Message):
    total    = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    in_chat  = conn.execute("SELECT COUNT(*) FROM chats WHERE ended=0").fetchone()[0]
    searching= conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    total_ch = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    await message.answer(
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: *{total}*\n"
        f"💬 Пар в чате: *{in_chat}*\n"
        f"🔍 В поиске: *{searching}*\n"
        f"🗂 Всего чатов: *{total_ch}*",
        parse_mode="Markdown"
    )

async def show_ref(uid, message: Message, bot: Bot):
    me   = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    u    = get_user(uid)
    await message.answer(
        f"🔗 *Ваша реферальная ссылка:*\n\n`{link}`\n\n"
        f"👥 Вы пригласили: *{u['ref_count']}* чел.",
        parse_mode="Markdown"
    )

# ═══════════════════════════════════════════════════════════════
#  ИНЛАЙН КНОПКИ
# ═══════════════════════════════════════════════════════════════

@router.callback_query()
async def callbacks(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    d   = call.data

    if d == "newname":
        name = rnd_name()
        conn.execute("UPDATE users SET anon_name=? WHERE user_id=?", (name, uid))
        conn.commit()
        await call.message.edit_text(f"✅ Новое имя: *{name}*", parse_mode="Markdown")
        await call.answer()
        return

    if d == "skip_rating":
        await call.message.edit_text("✖️ Оценка пропущена.")
        await call.answer()
        return

    # Оценка
    if d.startswith("rate_"):
        parts = d.split("_")
        pid, cid, score = int(parts[1]), int(parts[2]), int(parts[3])
        if conn.execute("SELECT 1 FROM ratings WHERE rater_id=? AND chat_id=?", (uid, cid)).fetchone():
            await call.answer("Вы уже оценили этот чат.", show_alert=True)
            return
        conn.execute("INSERT INTO ratings (rater_id,rated_id,chat_id,score) VALUES (?,?,?,?)", (uid, pid, cid, score))
        conn.execute("UPDATE users SET rating_sum=rating_sum+?, rating_count=rating_count+1 WHERE user_id=?", (score, pid))
        conn.commit()
        await call.message.edit_text(
            f"✅ Оценка: {'⭐'*score}\n\nХотите пожаловаться?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚨 Пожаловаться", callback_data=f"report_{pid}_{cid}"),
                InlineKeyboardButton(text="✖️ Нет",          callback_data="skip_rating"),
            ]])
        )
        await call.answer()
        return

    # Репорт
    if d.startswith("report_"):
        parts = d.split("_")
        pid, cid = int(parts[1]), int(parts[2])
        if conn.execute("SELECT 1 FROM reports WHERE reporter_id=? AND chat_id=?", (uid, cid)).fetchone():
            await call.answer("Вы уже жаловались на этот чат.", show_alert=True)
            return
        res = conn.execute("INSERT INTO reports (reporter_id,reported_id,chat_id) VALUES (?,?,?)", (uid, pid, cid))
        rid = res.lastrowid
        conn.commit()

        reporter = get_user(uid)
        reported = get_user(pid)
        dialog   = format_dialog(cid)

        admin_text = (
            f"🚨 *ЖАЛОБА #{rid}*\n\n"
            f"👤 От: *{reporter['anon_name']}* (`{uid}`)\n"
            f"🎯 На: *{reported['anon_name']}* (`{pid}`)\n\n"
            f"📋 *Диалог чата #{cid}:*\n"
            f"{'─'*28}\n{dialog}"
        )
        if ADMIN_ID:
            try:
                await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown", reply_markup=admin_kb(rid, pid))
            except Exception as e:
                logger.error(f"Admin error: {e}")

        await call.message.edit_text("✅ Жалоба отправлена администратору!")
        await call.answer()
        return

    # Админ: забанить
    if d.startswith("adm_ban_"):
        if uid != ADMIN_ID:
            await call.answer("Нет прав.", show_alert=True)
            return
        parts = d.split("_")
        rid, target = int(parts[2]), int(parts[3])
        conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (target,))
        conn.execute("UPDATE reports SET status='banned' WHERE id=?", (rid,))
        conn.commit()
        try:
            await bot.send_message(target, "🚫 Вы заблокированы администратором.")
        except: pass
        t = get_user(target)
        await call.message.edit_text(call.message.text + f"\n\n🔨 *{t['anon_name']} ЗАБАНЕН*", parse_mode="Markdown")
        await call.answer()
        return

    # Админ: пропустить
    if d.startswith("adm_skip_"):
        if uid != ADMIN_ID:
            await call.answer("Нет прав.", show_alert=True)
            return
        rid = int(d.split("_")[2])
        conn.execute("UPDATE reports SET status='skipped' WHERE id=?", (rid,))
        conn.commit()
        await call.message.edit_text(call.message.text + "\n\n✅ *Жалоба пропущена*", parse_mode="Markdown")
        await call.answer()
        return

    # Админ: закрыть
    if d.startswith("adm_close_"):
        if uid != ADMIN_ID:
            await call.answer("Нет прав.", show_alert=True)
            return
        rid = int(d.split("_")[2])
        conn.execute("UPDATE reports SET status='closed' WHERE id=?", (rid,))
        conn.commit()
        await call.message.edit_text(call.message.text + "\n\n🔒 *Проверка закрыта*", parse_mode="Markdown")
        await call.answer()
        return

    await call.answer()

# ═══════════════════════════════════════════════════════════════
#  КОМАНДЫ
# ═══════════════════════════════════════════════════════════════

@router.message(Command("find"))
async def find_cmd(message: Message, bot: Bot):
    await do_find(message.from_user.id, message, bot)

@router.message(Command("leave"))
async def leave_cmd(message: Message, bot: Bot):
    await do_leave(message.from_user.id, message, bot)

@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    total   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned  = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM reports WHERE status='pending'").fetchone()[0]
    await message.answer(
        f"🛡 *Панель админа*\n\n"
        f"👥 Пользователей: *{total}*\n"
        f"🚫 Забанено: *{banned}*\n"
        f"🚨 Жалоб (ожидают): *{pending}*",
        parse_mode="Markdown"
    )

@router.message(Command("unban"))
async def unban_cmd(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /unban <user_id>")
        return
    try:
        target = int(parts[1])
        conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (target,))
        conn.commit()
        await message.answer(f"✅ Пользователь {target} разбанен.")
        await bot.send_message(target, "✅ Ваш бан снят!")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher()
    dp.include_router(router)
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
