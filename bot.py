import asyncio
import logging
import random
import string
import sqlite3
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))

# ═══════════════════════════════════════════════════════════════
#  FSM СОСТОЯНИЯ
# ═══════════════════════════════════════════════════════════════
class Reg(StatesGroup):
    waiting_name = State()

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
        ts        TEXT DEFAULT (strftime('%H:%M', 'now', 'localtime'))
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
    return "\n".join(f"[{r['ts']}] {r['nick']}: {r['content']}" for r in rows)

def avg_rating(uid):
    u = get_user(uid)
    if not u or u["rating_count"] == 0:
        return "нет оценок"
    return f"{u['rating_sum']/u['rating_count']:.1f} ⭐ ({u['rating_count']} оценок)"

def get_all_user_ids():
    rows = conn.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
    return [r["user_id"] for r in rows]

def validate_name(name: str) -> tuple[bool, str]:
    name = name.strip()
    if len(name) < 2:
        return False, "слишком короткий (минимум 2 символа)"
    if len(name) > 20:
        return False, "слишком длинный (максимум 20 символов)"
    if not all(c.isalnum() or c in "_-" for c in name):
        return False, "только буквы, цифры, _ и -"
    return True, name

# ═══════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════

def main_menu(is_admin=False):
    rows = [
        [KeyboardButton(text="🔍 Найти чат"),     KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🔗 Реферальная"),    KeyboardButton(text="📊 Статистика")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🛡 Админ панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, persistent=True)

MENU_CHAT = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🚪 Покинуть чат")],
], resize_keyboard=True, persistent=True)

def rating_kb(partner_id, chat_id):
    def b(t, s): return InlineKeyboardButton(text=t, callback_data=f"rate_{partner_id}_{chat_id}_{s}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("⭐ 1", 1), b("⭐⭐ 2", 2), b("⭐⭐⭐ 3", 3)],
        [b("⭐⭐⭐⭐ 4", 4), b("⭐⭐⭐⭐⭐ 5", 5)],
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

def get_menu(uid):
    return main_menu(is_admin=(uid == ADMIN_ID))

# ── /start ──────────────────────────────────────────────────────
@router.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot):
    uid  = message.from_user.id
    args = message.text.split()[1] if len(message.text.split()) > 1 else ""

    if is_banned(uid):
        await message.answer("🚫 <b>Вы заблокированы.</b>", parse_mode="HTML")
        return

    user = get_user(uid)
    if user:
        await message.answer(
            f"╔═══════════════════╗\n"
            f"║  👋 С возвращением!  ║\n"
            f"╚═══════════════════╝\n\n"
            f"🎭 Ваш ник: <b>{user['anon_name']}</b>\n\n"
            f"Используй кнопки внизу 👇",
            parse_mode="HTML",
            reply_markup=get_menu(uid)
        )
        return

    # Новый пользователь — запросить ник
    ref_data = args if args.startswith("ref_") else ""
    await state.update_data(ref=ref_data)
    await state.set_state(Reg.waiting_name)
    await message.answer(
        "╔══════════════════════════╗\n"
        "║  🕵️ <b>АНОНИМНЫЙ ЧАТ</b>  ║\n"
        "╚══════════════════════════╝\n\n"
        "Привет! Здесь ты можешь общаться анонимно с незнакомцами.\n\n"
        "✏️ <b>Придумай себе ник:</b>\n"
        "<i>(от 2 до 20 символов, буквы и цифры)</i>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

# ── Ввод ника ───────────────────────────────────────────────────
@router.message(StateFilter(Reg.waiting_name), F.text)
async def set_name(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    ok, result = validate_name(message.text)
    if not ok:
        await message.answer(
            f"❌ <b>Неверный ник</b> — {result}.\n\nПопробуй ещё раз:",
            parse_mode="HTML"
        )
        return

    name = result
    data = await state.get_data()
    ref_arg = data.get("ref", "")

    ref_by = None
    if ref_arg.startswith("ref_"):
        try:
            ref_by = int(ref_arg[4:])
            if ref_by == uid:
                ref_by = None
        except ValueError:
            pass

    conn.execute("INSERT OR IGNORE INTO users (user_id,anon_name,referred_by) VALUES (?,?,?)", (uid, name, ref_by))
    conn.commit()

    if ref_by and get_user(ref_by):
        conn.execute("UPDATE users SET ref_count=ref_count+1 WHERE user_id=?", (ref_by,))
        conn.commit()
        ru = get_user(ref_by)
        try:
            await bot.send_message(
                ref_by,
                f"🎉 По вашей ссылке зарегистрировался <b>{name}</b>!\n"
                f"👥 Рефералов: <b>{ru['ref_count']}</b>",
                parse_mode="HTML"
            )
        except: pass

    await state.clear()
    await message.answer(
        f"✅ <b>Ник установлен!</b>\n\n"
        f"🎭 Ты в чате как: <b>{name}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🔍 Нажми <b>«Найти чат»</b> чтобы начать общаться!\n"
        f"━━━━━━━━━━━━━━━━━",
        parse_mode="HTML",
        reply_markup=get_menu(uid)
    )

# ═══════════════════════════════════════════════════════════════
#  ПОИСК / ВЫХОД
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

        text1 = (
            f"┌─────────────────────┐\n"
            f"│  ✅ <b>СОБЕСЕДНИК НАЙДЕН</b>  │\n"
            f"└─────────────────────┘\n\n"
            f"🎭 Ваш партнёр: <b>{u2['anon_name']}</b>\n\n"
            f"💬 Начинайте общаться!\n"
            f"<i>Нажмите «🚪 Покинуть чат» чтобы выйти</i>"
        )
        text2 = (
            f"┌─────────────────────┐\n"
            f"│  ✅ <b>СОБЕСЕДНИК НАЙДЕН</b>  │\n"
            f"└─────────────────────┘\n\n"
            f"🎭 Ваш партнёр: <b>{u1['anon_name']}</b>\n\n"
            f"💬 Начинайте общаться!\n"
            f"<i>Нажмите «🚪 Покинуть чат» чтобы выйти</i>"
        )
        await message.answer(text1, parse_mode="HTML", reply_markup=MENU_CHAT)
        await bot.send_message(pid, text2, parse_mode="HTML", reply_markup=MENU_CHAT)
    else:
        conn.execute("INSERT OR IGNORE INTO queue (user_id) VALUES (?)", (uid,))
        conn.commit()
        await message.answer(
            "🔍 <b>Ищем собеседника…</b>\n\n"
            "<i>Как только кто-то появится — чат начнётся автоматически!</i>",
            parse_mode="HTML",
            reply_markup=get_menu(uid)
        )

async def do_leave(uid, message: Message, bot: Bot):
    if in_queue(uid):
        conn.execute("DELETE FROM queue WHERE user_id=?", (uid,))
        conn.commit()
        await message.answer("✅ Поиск отменён.", reply_markup=get_menu(uid))
        return

    pid = get_partner(uid)
    if not pid:
        await message.answer("❗ Вы не в чате.", reply_markup=get_menu(uid))
        return

    user    = get_user(uid)
    chat_id = get_active_chat_id(uid)
    conn.execute("UPDATE chats SET ended=1 WHERE id=?", (chat_id,))
    conn.commit()

    end_text = (
        "┌──────────────────┐\n"
        "│  👋 <b>ЧАТ ЗАВЕРШЁН</b>  │\n"
        "└──────────────────┘"
    )

    await message.answer(end_text, parse_mode="HTML", reply_markup=get_menu(uid))
    await message.answer("⭐ <b>Оцените собеседника:</b>", parse_mode="HTML", reply_markup=rating_kb(pid, chat_id))

    await bot.send_message(
        pid,
        f"{end_text}\n\n<i>Собеседник <b>{user['anon_name']}</b> покинул чат.</i>",
        parse_mode="HTML",
        reply_markup=get_menu(pid)
    )
    await bot.send_message(pid, "⭐ <b>Оцените собеседника:</b>", parse_mode="HTML", reply_markup=rating_kb(uid, chat_id))

# ═══════════════════════════════════════════════════════════════
#  ПЕРЕСЫЛКА
# ═══════════════════════════════════════════════════════════════

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
#  ПРОФИЛЬ / СТАТИСТИКА / РЕФЕРАЛЬНАЯ / АДМИН
# ═══════════════════════════════════════════════════════════════

async def show_profile(uid, message: Message):
    u  = get_user(uid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Сменить ник", callback_data="change_name")
    ]])
    await message.answer(
        f"┌──────────────────────┐\n"
        f"│      👤 <b>ВАШ ПРОФИЛЬ</b>      │\n"
        f"└──────────────────────┘\n\n"
        f"🎭 Ник: <b>{u['anon_name']}</b>\n"
        f"💬 Чатов проведено: <b>{u['chats_count']}</b>\n"
        f"✉️ Сообщений: <b>{u['messages_sent']}</b>\n"
        f"⭐ Рейтинг: <b>{avg_rating(uid)}</b>\n"
        f"👥 Рефералов: <b>{u['ref_count']}</b>",
        parse_mode="HTML",
        reply_markup=kb
    )

async def show_stats(message: Message):
    total    = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    in_chat  = conn.execute("SELECT COUNT(*) FROM chats WHERE ended=0").fetchone()[0]
    searching= conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    total_ch = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    await message.answer(
        f"┌──────────────────────┐\n"
        f"│      📊 <b>СТАТИСТИКА</b>       │\n"
        f"└──────────────────────┘\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"💬 Пар в чате сейчас: <b>{in_chat}</b>\n"
        f"🔍 В поиске: <b>{searching}</b>\n"
        f"🗂 Всего чатов: <b>{total_ch}</b>",
        parse_mode="HTML"
    )

async def show_ref(uid, message: Message, bot: Bot):
    me   = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    u    = get_user(uid)
    await message.answer(
        f"┌──────────────────────┐\n"
        f"│    🔗 <b>РЕФЕРАЛЬНАЯ</b>       │\n"
        f"└──────────────────────┘\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"👥 Вы пригласили: <b>{u['ref_count']}</b> чел.\n\n"
        f"<i>Поделитесь ссылкой и приглашайте друзей!</i>",
        parse_mode="HTML"
    )

async def show_admin(uid, message: Message):
    if uid != ADMIN_ID:
        return
    total   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned  = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM reports WHERE status='pending'").fetchone()[0]
    in_chat = conn.execute("SELECT COUNT(*) FROM chats WHERE ended=0").fetchone()[0]
    search  = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    total_r = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
    ])
    await message.answer(
        f"┌──────────────────────┐\n"
        f"│    🛡 <b>ПАНЕЛЬ АДМИНА</b>     │\n"
        f"└──────────────────────┘\n\n"
        f"👥 Пользователей: <b>{total}</b>\n"
        f"🚫 Забанено: <b>{banned}</b>\n"
        f"💬 В чате: <b>{in_chat}</b> пар\n"
        f"🔍 В поиске: <b>{search}</b>\n"
        f"🚨 Жалоб (ожидают): <b>{pending}</b>\n"
        f"📋 Всего жалоб: <b>{total_r}</b>\n\n"
        f"<i>Команды: /unban &lt;id&gt; · /ban &lt;id&gt;</i>",
        parse_mode="HTML",
        reply_markup=kb
    )

# ═══════════════════════════════════════════════════════════════
#  ОБРАБОТКА ТЕКСТА
# ═══════════════════════════════════════════════════════════════

@router.message(F.text)
async def handle_text(message: Message, state: FSMContext, bot: Bot):
    uid  = message.from_user.id
    text = message.text

    if is_banned(uid):
        await message.answer("🚫 Вы заблокированы.")
        return

    user = get_user(uid)
    if not user:
        await message.answer("Напишите /start чтобы начать.")
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
    elif text == "🛡 Админ панель":
        await show_admin(uid, message)
    else:
        pid = get_partner(uid)
        if not pid:
            if in_queue(uid):
                await message.answer("🔍 Ещё ищем собеседника…")
            else:
                await message.answer("❗ Вы не в чате. Нажмите «🔍 Найти чат».", reply_markup=get_menu(uid))
            return
        await relay(message, bot, uid, pid)

# ── Медиа ───────────────────────────────────────────────────────
@router.message(F.photo | F.video | F.voice | F.sticker | F.animation | F.document | F.video_note | F.audio)
async def handle_media(message: Message, bot: Bot):
    uid = message.from_user.id
    if is_banned(uid):
        return
    if not get_user(uid):
        return
    pid = get_partner(uid)
    if not pid:
        if in_queue(uid):
            await message.answer("🔍 Ещё ищем…")
        else:
            await message.answer("❗ Вы не в чате.", reply_markup=get_menu(uid))
        return
    await relay(message, bot, uid, pid)

# ═══════════════════════════════════════════════════════════════
#  ИНЛАЙН КНОПКИ
# ═══════════════════════════════════════════════════════════════

# FSM для смены ника и рассылки
class ChangeName(StatesGroup):
    waiting = State()

class Broadcast(StatesGroup):
    waiting = State()

@router.callback_query()
async def callbacks(call: CallbackQuery, state: FSMContext, bot: Bot):
    uid = call.from_user.id
    d   = call.data

    # Сменить ник
    if d == "change_name":
        await state.set_state(ChangeName.waiting)
        await call.message.answer(
            "✏️ <b>Введите новый ник:</b>\n<i>(от 2 до 20 символов)</i>",
            parse_mode="HTML"
        )
        await call.answer()
        return

    # Рассылка (только для смены ника)
    if d == "adm_broadcast":
        if uid != ADMIN_ID:
            await call.answer("Нет прав.", show_alert=True)
            return
        await state.set_state(Broadcast.waiting)
        await call.message.answer("📢 <b>Введите текст рассылки:</b>", parse_mode="HTML")
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
            f"✅ Оценка поставлена: {'⭐'*score}\n\n<i>Хотите пожаловаться?</i>",
            parse_mode="HTML",
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
            await call.answer("Вы уже жаловались.", show_alert=True)
            return
        res = conn.execute("INSERT INTO reports (reporter_id,reported_id,chat_id) VALUES (?,?,?)", (uid, pid, cid))
        rid = res.lastrowid
        conn.commit()

        reporter = get_user(uid)
        reported = get_user(pid)
        dialog   = format_dialog(cid)

        admin_text = (
            f"🚨 <b>ЖАЛОБА #{rid}</b>\n\n"
            f"👤 От: <b>{reporter['anon_name']}</b> (<code>{uid}</code>)\n"
            f"🎯 На: <b>{reported['anon_name']}</b> (<code>{pid}</code>)\n\n"
            f"📋 <b>Диалог чата #{cid}:</b>\n"
            f"{'─'*28}\n<code>{dialog}</code>"
        )
        if ADMIN_ID:
            try:
                await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=admin_kb(rid, pid))
            except Exception as e:
                logger.error(f"Admin error: {e}")

        await call.message.edit_text("✅ <b>Жалоба отправлена администратору!</b>", parse_mode="HTML")
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
            await bot.send_message(target, "🚫 <b>Вы заблокированы администратором.</b>", parse_mode="HTML")
        except: pass
        t = get_user(target)
        await call.message.edit_text(call.message.text + f"\n\n🔨 <b>{t['anon_name']} ЗАБАНЕН</b>", parse_mode="HTML")
        await call.answer("Забанен ✅")
        return

    if d.startswith("adm_skip_"):
        if uid != ADMIN_ID:
            await call.answer("Нет прав.", show_alert=True)
            return
        rid = int(d.split("_")[2])
        conn.execute("UPDATE reports SET status='skipped' WHERE id=?", (rid,))
        conn.commit()
        await call.message.edit_text(call.message.text + "\n\n✅ <b>Жалоба пропущена</b>", parse_mode="HTML")
        await call.answer()
        return

    if d.startswith("adm_close_"):
        if uid != ADMIN_ID:
            await call.answer("Нет прав.", show_alert=True)
            return
        rid = int(d.split("_")[2])
        conn.execute("UPDATE reports SET status='closed' WHERE id=?", (rid,))
        conn.commit()
        await call.message.edit_text(call.message.text + "\n\n🔒 <b>Проверка закрыта</b>", parse_mode="HTML")
        await call.answer()
        return

    await call.answer()

# ── Обработка смены ника ────────────────────────────────────────
@router.message(StateFilter(ChangeName.waiting), F.text)
async def process_new_name(message: Message, state: FSMContext):
    uid = message.from_user.id
    ok, result = validate_name(message.text)
    if not ok:
        await message.answer(f"❌ Неверный ник — {result}. Попробуй ещё раз:")
        return
    conn.execute("UPDATE users SET anon_name=? WHERE user_id=?", (result, uid))
    conn.commit()
    await state.clear()
    await message.answer(f"✅ <b>Новый ник: {result}</b>", parse_mode="HTML", reply_markup=get_menu(uid))

# ── Рассылка ────────────────────────────────────────────────────
@router.message(StateFilter(Broadcast.waiting), F.text)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text
    await state.clear()
    user_ids = get_all_user_ids()
    ok, fail = 0, 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, f"📢 <b>Сообщение от администратора:</b>\n\n{text}", parse_mode="HTML")
            ok += 1
        except:
            fail += 1
    await message.answer(f"✅ Рассылка завершена!\n📨 Доставлено: {ok}\n❌ Ошибок: {fail}")

# ═══════════════════════════════════════════════════════════════
#  КОМАНДЫ
# ═══════════════════════════════════════════════════════════════

@router.message(Command("find"))
async def find_cmd(message: Message, bot: Bot):
    if not get_user(message.from_user.id):
        await message.answer("Сначала /start")
        return
    await do_find(message.from_user.id, message, bot)

@router.message(Command("leave"))
async def leave_cmd(message: Message, bot: Bot):
    await do_leave(message.from_user.id, message, bot)

@router.message(Command("admin"))
async def admin_cmd(message: Message):
    await show_admin(message.from_user.id, message)

@router.message(Command("ban"))
async def ban_cmd(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /ban <user_id>")
        return
    try:
        target = int(parts[1])
        conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (target,))
        conn.commit()
        await message.answer(f"✅ Пользователь {target} забанен.")
        await bot.send_message(target, "🚫 <b>Вы заблокированы.</b>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

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
        await bot.send_message(target, "✅ <b>Ваш бан снят!</b>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

# ═══════════════════════════════════════════════════════════════
#  АВТОРАССЫЛКА КАЖДЫЕ 4 ЧАСА
# ═══════════════════════════════════════════════════════════════

PROMO_MESSAGES = [
    "💬 <b>Скучно?</b>\n\nЗаходи в анонимный чат — тут всегда есть кто-то интересный!\nНажми «🔍 Найти чат» и начни общаться прямо сейчас 👇",
    "🕵️ <b>Анонимный чат ждёт тебя!</b>\n\nТысячи людей общаются прямо сейчас.\nМожет твой следующий собеседник станет лучшим другом? 👀",
    "🔥 <b>Не сиди в тишине!</b>\n\nЗдесь можно говорить обо всём — никто не узнает кто ты.\nНажми «🔍 Найти чат» и погнали! 🚀",
    "🌙 <b>Ночью скучно?</b>\n\nМы тут! Анонимный чат работает 24/7.\nНайди собеседника прямо сейчас 💬",
    "⚡ <b>Новые знакомства каждый день!</b>\n\nОбщайся анонимно, без лишних вопросов.\nНажми «🔍 Найти чат» — это бесплатно! 😉",
]

async def send_promo(bot: Bot):
    while True:
        await asyncio.sleep(4 * 60 * 60)  # 4 часа
        user_ids = get_all_user_ids()
        text     = random.choice(PROMO_MESSAGES)
        sent = 0
        for uid in user_ids:
            # Не отправлять тем, кто сейчас в чате
            if get_partner(uid):
                continue
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)  # антифлуд
            except:
                pass
        logger.info(f"Promo sent to {sent} users")

# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("✅ Бот запущен!")
    asyncio.create_task(send_promo(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
