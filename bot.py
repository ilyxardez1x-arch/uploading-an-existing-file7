import logging
import random
import string
import sqlite3
import os
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))   # свой Telegram ID

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
        started   TEXT DEFAULT (datetime('now')),
        ended     INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS messages (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id   INTEGER,
        sender_id INTEGER,
        nick      TEXT,
        content   TEXT,
        ts        TEXT DEFAULT (datetime('now'))
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
    lines = []
    for r in rows:
        ts = r["ts"][11:16]
        lines.append(f"[{ts}] *{r['nick']}*: {r['content']}")
    return "\n".join(lines)

def avg_rating(uid):
    u = get_user(uid)
    if not u or u["rating_count"] == 0:
        return "нет оценок"
    avg = u["rating_sum"] / u["rating_count"]
    return f"{avg:.1f} ⭐  ({u['rating_count']} оценок)"

def ref_link(uid, bot_username):
    return f"https://t.me/{bot_username}?start=ref_{uid}"

# ═══════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════

MENU_MAIN = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔍 Найти чат"),     KeyboardButton("👤 Профиль")],
        [KeyboardButton("🔗 Реферальная"),   KeyboardButton("📊 Статистика")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

MENU_CHAT = ReplyKeyboardMarkup(
    [[KeyboardButton("🚪 Покинуть чат")]],
    resize_keyboard=True,
    is_persistent=True,
)

def rating_kb(partner_id, chat_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐1",     callback_data=f"rate_{partner_id}_{chat_id}_1"),
            InlineKeyboardButton("⭐⭐2",   callback_data=f"rate_{partner_id}_{chat_id}_2"),
            InlineKeyboardButton("⭐⭐⭐3", callback_data=f"rate_{partner_id}_{chat_id}_3"),
        ],
        [
            InlineKeyboardButton("⭐⭐⭐⭐4",   callback_data=f"rate_{partner_id}_{chat_id}_4"),
            InlineKeyboardButton("⭐⭐⭐⭐⭐5", callback_data=f"rate_{partner_id}_{chat_id}_5"),
        ],
        [InlineKeyboardButton("🚨 Пожаловаться", callback_data=f"report_{partner_id}_{chat_id}")],
        [InlineKeyboardButton("✖️ Пропустить",   callback_data="skip_rating")],
    ])

def admin_kb(report_id, reported_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔨 Забанить",         callback_data=f"adm_ban_{report_id}_{reported_id}"),
        InlineKeyboardButton("✅ Пропустить",        callback_data=f"adm_skip_{report_id}"),
        InlineKeyboardButton("🔒 Закрыть проверку", callback_data=f"adm_close_{report_id}"),
    ]])

# ═══════════════════════════════════════════════════════════════
#  /start
# ═══════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    args = ctx.args

    if is_banned(uid):
        await update.message.reply_text("🚫 Вы заблокированы.")
        return

    user = get_user(uid)
    if not user:
        name   = rnd_name()
        ref_by = None
        if args and args[0].startswith("ref_"):
            try:
                ref_by = int(args[0][4:])
                if ref_by == uid:
                    ref_by = None
            except ValueError:
                pass

        conn.execute(
            "INSERT INTO users (user_id,anon_name,referred_by) VALUES (?,?,?)",
            (uid, name, ref_by)
        )
        conn.commit()

        if ref_by and get_user(ref_by):
            conn.execute("UPDATE users SET ref_count=ref_count+1 WHERE user_id=?", (ref_by,))
            conn.commit()
            ref_user = get_user(ref_by)
            await ctx.bot.send_message(
                ref_by,
                f"🎉 По вашей ссылке зарегистрировался новый пользователь!\n"
                f"👥 Всего рефералов: *{ref_user['ref_count']}*",
                parse_mode="Markdown"
            )

        text = (
            f"👋 Добро пожаловать в *Анонимный Чат*!\n\n"
            f"Ваше анонимное имя: *{name}*\n\n"
            "Используй кнопки внизу экрана для навигации 👇"
        )
    else:
        text = f"👋 С возвращением, *{user['anon_name']}*!\nКнопки внизу экрана 👇"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MENU_MAIN)

# ═══════════════════════════════════════════════════════════════
#  ПОИСК / ВЫХОД
# ═══════════════════════════════════════════════════════════════

async def do_find(uid, message, ctx):
    if is_banned(uid):
        await message.reply_text("🚫 Вы заблокированы.")
        return
    if get_partner(uid):
        await message.reply_text("❗ Вы уже в чате. Нажмите «🚪 Покинуть чат».", reply_markup=MENU_CHAT)
        return
    if in_queue(uid):
        await message.reply_text("🔍 Уже ищем, подождите…")
        return

    waiting = conn.execute("SELECT user_id FROM queue WHERE user_id!=? LIMIT 1", (uid,)).fetchone()
    if waiting:
        pid = waiting["user_id"]
        conn.execute("DELETE FROM queue WHERE user_id=?", (pid,))
        conn.execute("INSERT INTO chats (user1_id,user2_id) VALUES (?,?)", (uid, pid))
        conn.execute("UPDATE users SET chats_count=chats_count+1 WHERE user_id IN (?,?)", (uid, pid))
        conn.commit()

        u1, u2 = get_user(uid), get_user(pid)
        await message.reply_text(
            f"✅ Собеседник найден!\nПартнёр: *{u2['anon_name']}*\n\n💬 Пишите!",
            parse_mode="Markdown", reply_markup=MENU_CHAT
        )
        await ctx.bot.send_message(
            pid,
            f"✅ Собеседник найден!\nПартнёр: *{u1['anon_name']}*\n\n💬 Пишите!",
            parse_mode="Markdown", reply_markup=MENU_CHAT
        )
    else:
        conn.execute("INSERT OR IGNORE INTO queue (user_id) VALUES (?)", (uid,))
        conn.commit()
        await message.reply_text("🔍 Ищем собеседника…\nКак только кто-то появится — чат начнётся!", reply_markup=MENU_MAIN)

async def do_leave(uid, message, ctx):
    if in_queue(uid):
        conn.execute("DELETE FROM queue WHERE user_id=?", (uid,))
        conn.commit()
        await message.reply_text("✅ Поиск отменён.", reply_markup=MENU_MAIN)
        return

    pid = get_partner(uid)
    if not pid:
        await message.reply_text("❗ Вы не в чате.", reply_markup=MENU_MAIN)
        return

    user    = get_user(uid)
    chat_id = get_active_chat_id(uid)

    conn.execute("UPDATE chats SET ended=1 WHERE id=?", (chat_id,))
    conn.commit()

    # Рейтинг тому, кто вышел
    await message.reply_text("👋 Чат завершён!", reply_markup=MENU_MAIN)
    await message.reply_text("⭐ Оцените собеседника:", reply_markup=rating_kb(pid, chat_id))

    # Рейтинг партнёру
    await ctx.bot.send_message(
        pid,
        f"👋 Собеседник *{user['anon_name']}* покинул чат.\n\n⭐ Оцените его:",
        parse_mode="Markdown",
        reply_markup=MENU_MAIN
    )
    await ctx.bot.send_message(pid, "Выберите оценку:", reply_markup=rating_kb(uid, chat_id))

# ═══════════════════════════════════════════════════════════════
#  ПРОФИЛЬ / СТАТИСТИКА / РЕФЕРАЛЬНАЯ
# ═══════════════════════════════════════════════════════════════

async def show_profile(uid, message):
    u  = get_user(uid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Сменить имя", callback_data="newname")]])
    await message.reply_text(
        f"👤 *Ваш профиль*\n\n"
        f"🎭 Имя: *{u['anon_name']}*\n"
        f"💬 Чатов: *{u['chats_count']}*\n"
        f"✉️ Сообщений: *{u['messages_sent']}*\n"
        f"⭐ Рейтинг: *{avg_rating(uid)}*\n"
        f"👥 Рефералов: *{u['ref_count']}*",
        parse_mode="Markdown", reply_markup=kb
    )

async def show_stats(message):
    total    = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    in_chat  = conn.execute("SELECT COUNT(*) FROM chats WHERE ended=0").fetchone()[0]
    searching= conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    total_ch = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    await message.reply_text(
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: *{total}*\n"
        f"💬 Пар в чате: *{in_chat}*\n"
        f"🔍 В поиске: *{searching}*\n"
        f"🗂 Всего чатов: *{total_ch}*",
        parse_mode="Markdown"
    )

async def show_ref(uid, message, ctx):
    bot_me = await ctx.bot.get_me()
    link   = ref_link(uid, bot_me.username)
    u      = get_user(uid)
    await message.reply_text(
        f"🔗 *Ваша реферальная ссылка:*\n\n`{link}`\n\n"
        f"👥 Вы пригласили: *{u['ref_count']}* чел.\n\n"
        "Поделитесь ссылкой — и друг автоматически привяжется к вам!",
        parse_mode="Markdown"
    )

# ═══════════════════════════════════════════════════════════════
#  ПЕРЕСЫЛКА
# ═══════════════════════════════════════════════════════════════

async def relay_msg(update, ctx, uid, pid):
    msg     = update.message
    user    = get_user(uid)
    chat_id = get_active_chat_id(uid)

    conn.execute("UPDATE users SET messages_sent=messages_sent+1 WHERE user_id=?", (uid,))
    conn.commit()

    label = None
    try:
        if msg.text:
            await ctx.bot.send_message(pid, f"💬 {msg.text}")
            label = msg.text
        elif msg.photo:
            await ctx.bot.send_photo(pid, msg.photo[-1].file_id, caption=msg.caption or "")
            label = f"[📷 Фото]{' | '+msg.caption if msg.caption else ''}"
        elif msg.video:
            await ctx.bot.send_video(pid, msg.video.file_id, caption=msg.caption or "")
            label = f"[🎥 Видео]{' | '+msg.caption if msg.caption else ''}"
        elif msg.voice:
            await ctx.bot.send_voice(pid, msg.voice.file_id)
            label = "[🎤 Голосовое]"
        elif msg.sticker:
            await ctx.bot.send_sticker(pid, msg.sticker.file_id)
            label = f"[🎭 Стикер {msg.sticker.emoji or ''}]"
        elif msg.animation:
            await ctx.bot.send_animation(pid, msg.animation.file_id)
            label = "[GIF]"
        elif msg.document:
            await ctx.bot.send_document(pid, msg.document.file_id, caption=msg.caption or "")
            label = f"[📎 {msg.document.file_name}]"
        elif msg.video_note:
            await ctx.bot.send_video_note(pid, msg.video_note.file_id)
            label = "[⭕ Видеосообщение]"
        elif msg.audio:
            await ctx.bot.send_audio(pid, msg.audio.file_id)
            label = "[🎵 Аудио]"
    except Exception as e:
        logger.error(f"Relay error: {e}")

    if chat_id and label:
        save_msg(chat_id, uid, user["anon_name"], label)

# ═══════════════════════════════════════════════════════════════
#  ОБРАБОТКА ТЕКСТОВЫХ СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text

    if is_banned(uid):
        await update.message.reply_text("🚫 Вы заблокированы.")
        return

    if text == "🔍 Найти чат":
        await do_find(uid, update.message, ctx)
    elif text == "🚪 Покинуть чат":
        await do_leave(uid, update.message, ctx)
    elif text == "👤 Профиль":
        await show_profile(uid, update.message)
    elif text == "📊 Статистика":
        await show_stats(update.message)
    elif text == "🔗 Реферальная":
        await show_ref(uid, update.message, ctx)
    else:
        # Обычное сообщение → пересылка
        pid = get_partner(uid)
        if not pid:
            if in_queue(uid):
                await update.message.reply_text("🔍 Ещё ищем собеседника…")
            else:
                await update.message.reply_text("❗ Вы не в чате. Нажмите «🔍 Найти чат».", reply_markup=MENU_MAIN)
            return
        await relay_msg(update, ctx, uid, pid)

# ═══════════════════════════════════════════════════════════════
#  МЕДИА
# ═══════════════════════════════════════════════════════════════

async def handle_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_banned(uid):
        return
    pid = get_partner(uid)
    if not pid:
        if in_queue(uid):
            await update.message.reply_text("🔍 Ещё ищем…")
        else:
            await update.message.reply_text("❗ Вы не в чате.", reply_markup=MENU_MAIN)
        return
    await relay_msg(update, ctx, uid, pid)

# ═══════════════════════════════════════════════════════════════
#  INLINE КНОПКИ
# ═══════════════════════════════════════════════════════════════

async def callbacks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    d   = q.data

    # Смена имени
    if d == "newname":
        name = rnd_name()
        conn.execute("UPDATE users SET anon_name=? WHERE user_id=?", (name, uid))
        conn.commit()
        await q.edit_message_text(f"✅ Новое имя: *{name}*", parse_mode="Markdown")
        return

    if d == "skip_rating":
        await q.edit_message_text("✖️ Оценка пропущена.")
        return

    # Оценка   rate_partnerID_chatID_score
    if d.startswith("rate_"):
        parts = d.split("_")
        pid, cid, score = int(parts[1]), int(parts[2]), int(parts[3])

        if conn.execute("SELECT 1 FROM ratings WHERE rater_id=? AND chat_id=?", (uid, cid)).fetchone():
            await q.edit_message_text("❗ Вы уже оценили этот чат.")
            return

        conn.execute("INSERT INTO ratings (rater_id,rated_id,chat_id,score) VALUES (?,?,?,?)", (uid, pid, cid, score))
        conn.execute("UPDATE users SET rating_sum=rating_sum+?, rating_count=rating_count+1 WHERE user_id=?", (score, pid))
        conn.commit()

        await q.edit_message_text(
            f"✅ Оценка поставлена: {'⭐'*score}\n\nХотите пожаловаться?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚨 Пожаловаться", callback_data=f"report_{pid}_{cid}"),
                InlineKeyboardButton("✖️ Нет",          callback_data="skip_rating"),
            ]])
        )
        return

    # Репорт   report_partnerID_chatID
    if d.startswith("report_"):
        parts = d.split("_")
        pid, cid = int(parts[1]), int(parts[2])

        if conn.execute("SELECT 1 FROM reports WHERE reporter_id=? AND chat_id=?", (uid, cid)).fetchone():
            await q.edit_message_text("❗ Вы уже подавали жалобу на этот чат.")
            return

        res = conn.execute(
            "INSERT INTO reports (reporter_id,reported_id,chat_id) VALUES (?,?,?)",
            (uid, pid, cid)
        )
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
            f"{'─'*28}\n"
            f"{dialog}"
        )

        if ADMIN_ID:
            try:
                await ctx.bot.send_message(
                    ADMIN_ID, admin_text,
                    parse_mode="Markdown",
                    reply_markup=admin_kb(rid, pid)
                )
            except Exception as e:
                logger.error(f"Admin msg error: {e}")

        await q.edit_message_text("✅ Жалоба отправлена администратору!")
        return

    # Админ: забанить   adm_ban_reportID_userID
    if d.startswith("adm_ban_"):
        if uid != ADMIN_ID:
            await q.answer("Нет прав.", show_alert=True)
            return
        parts = d.split("_")
        rid, target = int(parts[2]), int(parts[3])
        conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (target,))
        conn.execute("UPDATE reports SET status='banned' WHERE id=?", (rid,))
        conn.commit()
        try:
            await ctx.bot.send_message(target, "🚫 Вы были заблокированы администратором.")
        except: pass
        t = get_user(target)
        await q.edit_message_text(
            q.message.text + f"\n\n🔨 *{t['anon_name']} ЗАБАНЕН*",
            parse_mode="Markdown"
        )
        return

    # Админ: пропустить   adm_skip_reportID
    if d.startswith("adm_skip_"):
        if uid != ADMIN_ID:
            await q.answer("Нет прав.", show_alert=True)
            return
        rid = int(d.split("_")[2])
        conn.execute("UPDATE reports SET status='skipped' WHERE id=?", (rid,))
        conn.commit()
        await q.edit_message_text(q.message.text + "\n\n✅ *Жалоба пропущена*", parse_mode="Markdown")
        return

    # Админ: закрыть проверку   adm_close_reportID
    if d.startswith("adm_close_"):
        if uid != ADMIN_ID:
            await q.answer("Нет прав.", show_alert=True)
            return
        rid = int(d.split("_")[2])
        conn.execute("UPDATE reports SET status='closed' WHERE id=?", (rid,))
        conn.commit()
        await q.edit_message_text(q.message.text + "\n\n🔒 *Проверка закрыта*", parse_mode="Markdown")
        return

# ═══════════════════════════════════════════════════════════════
#  КОМАНДЫ
# ═══════════════════════════════════════════════════════════════

async def find_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await do_find(update.effective_user.id, update.message, ctx)

async def leave_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await do_leave(update.effective_user.id, update.message, ctx)

async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    total   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned  = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM reports WHERE status='pending'").fetchone()[0]
    total_r = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    await update.message.reply_text(
        f"🛡 *Панель администратора*\n\n"
        f"👥 Пользователей: *{total}*\n"
        f"🚫 Забанено: *{banned}*\n"
        f"🚨 Жалоб (ожидают): *{pending}*\n"
        f"📋 Всего жалоб: *{total_r}*",
        parse_mode="Markdown"
    )

async def unban_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /unban <user_id>")
        return
    try:
        target = int(ctx.args[0])
        conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (target,))
        conn.commit()
        await update.message.reply_text(f"✅ Пользователь {target} разбанен.")
        await ctx.bot.send_message(target, "✅ Ваш бан снят. Добро пожаловать обратно!")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════

app = Application.builder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("find",  find_cmd))
app.add_handler(CommandHandler("leave", leave_cmd))
app.add_handler(CommandHandler("admin", admin_cmd))
app.add_handler(CommandHandler("unban", unban_cmd))
app.add_handler(CallbackQueryHandler(callbacks))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(
    filters.PHOTO | filters.VIDEO | filters.VOICE | filters.STICKER |
    filters.ANIMATION | filters.Document.ALL | filters.VIDEO_NOTE | filters.AUDIO,
    handle_media
))

logger.info("Бот запущен!")
app.run_polling()
