import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === НАЛАШТУВАННЯ ===
BOT_TOKEN = "8657527520:AAHOYz-jsgN4rrTA41Z-2NU2rCMO1paGvJw"
STAROSTA_ID = 741239201
GEMINI_API_KEY = "AQ.Ab8RN6Kse3xWmCJdd1MnUy3ShFcrsF4JV5ZeR1pk18TgwJ04TQ"

KYIV_TZ = ZoneInfo("Europe/Kyiv")
DB_PATH = "starosta.db"

DAYS_UA = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")


# === СТАНИ (FSM) ===
class AdminStates(StatesGroup):
    waiting_for_announcement = State()
    waiting_for_file = State()
    waiting_for_file_subject = State()
    # Фото розкладу для ШІ
    waiting_for_schedule_photo = State()
    # Додавання пари вручну
    add_pair_day = State()
    add_pair_week = State()
    add_pair_type = State()
    add_pair_time = State()
    add_pair_subject = State()
    add_pair_teacher = State()
    add_pair_room = State()
    add_pair_link = State()
    # Домашнє завдання
    hw_subject = State()
    hw_text = State()
    hw_file = State()


# === БАЗА ДАНИХ ТА МІГРАЦІЯ ===
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS students (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT,
                file_name TEXT,
                subject TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                date TEXT,
                pair_time TEXT,
                user_id INTEGER,
                full_name TEXT,
                status TEXT,
                PRIMARY KEY (date, pair_time, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_idx INTEGER,
                week_type TEXT DEFAULT 'all',
                pair_type TEXT DEFAULT 'Пр',
                start_time TEXT,
                end_time TEXT DEFAULT '',
                subject TEXT,
                teacher TEXT DEFAULT '',
                room TEXT DEFAULT '',
                link TEXT DEFAULT '',
                poll_enabled INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS homework (
                subject TEXT PRIMARY KEY,
                task_text TEXT,
                file_id TEXT,
                file_type TEXT,
                updated_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('week_offset', '0')")

        # Автоматичне додавання нових колонок, якщо таблиця schedule залишилась від старої версії
        cursor = await db.execute("PRAGMA table_info(schedule)")
        cols = [row[1] for row in await cursor.fetchall()]
        migrations = {
            "week_type": "TEXT DEFAULT 'all'",
            "pair_type": "TEXT DEFAULT 'Пр'",
            "start_time": "TEXT DEFAULT '09:00'",
            "end_time": "TEXT DEFAULT ''",
            "teacher": "TEXT DEFAULT ''",
            "room": "TEXT DEFAULT ''",
            "link": "TEXT DEFAULT ''",
            "poll_enabled": "INTEGER DEFAULT 1",
        }
        for col_name, col_def in migrations.items():
            if col_name not in cols:
                await db.execute(f"ALTER TABLE schedule ADD COLUMN {col_name} {col_def}")

        # Якщо у старій таблиці час був у колонці pair_time — переносимо в start_time
        if "pair_time" in cols:
            await db.execute("UPDATE schedule SET start_time = pair_time WHERE start_time IS NULL OR start_time = '09:00'")

        await db.commit()


async def send_db_backup():
    """Тихий бекап бази старості, щоб дані не губилися при перезапуску безкоштовного Render"""
    try:
        if os.path.exists(DB_PATH):
            doc = FSInputFile(DB_PATH, filename="starosta_backup.db")
            await bot.send_document(
                chat_id=STAROSTA_ID,
                document=doc,
                caption="💾 Авто-бекап бази даних. Якщо Render перезавантажиться, просто надішли цей файл боту для миттєвого відновлення.",
                disable_notification=True,
            )
    except Exception as e:
        logging.error(f"Помилка бекапу: {e}")


# === ВИЗНАЧЕННЯ ТИЖНЯ (ЧИСЕЛЬНИК / ЗНАМЕННИК) ===
async def get_current_week_type() -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'week_offset'")
        row = await cursor.fetchone()
        offset = int(row[0]) if row else 0

    iso_week = datetime.now(KYIV_TZ).isocalendar()[1]
    is_numerator = ((iso_week + offset) % 2 == 1)
    return "num" if is_numerator else "den"


def week_label(week_type: str) -> str:
    return {
        "num": "Чисельник",
        "den": "Знаменник",
        "all": "Кожен тиждень",
    }.get(week_type, "Кожен тиждень")


# === КЛАВІАТУРИ ===
def main_kb(is_admin=False):
    kb = [
        [KeyboardButton(text="📅 Розклад"), KeyboardButton(text="📝 Домашнє завдання")],
        [KeyboardButton(text="📚 Матеріали")],
    ]
    if is_admin:
        kb.append([KeyboardButton(text="📸 Завантажити фото розкладу")])
        kb.append([KeyboardButton(text="➕ Додати пару"), KeyboardButton(text="🗑 Видалити пару")])
        kb.append([KeyboardButton(text="✏️ Додати ДЗ"), KeyboardButton(text="➕ Додати файл")])
        kb.append([KeyboardButton(text="🔄 Перемкнути тиждень"), KeyboardButton(text="📢 Оголошення")])
        kb.append([KeyboardButton(text="📊 Звіт відвідуваності")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Скасувати")]],
        resize_keyboard=True,
    )


def skip_kb(skip_text="⏭ Пропустити"):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=skip_text)], [KeyboardButton(text="❌ Скасувати")]],
        resize_keyboard=True,
    )


def week_choice_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔁 Кожен тиждень")],
            [KeyboardButton(text="🔼 Тільки Чисельник"), KeyboardButton(text="🔽 Тільки Знаменник")],
            [KeyboardButton(text="❌ Скасувати")],
        ],
        resize_keyboard=True,
    )


def type_choice_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔬 Пр (Практичне)"), KeyboardButton(text="📖 Лек (Лекція)")],
            [KeyboardButton(text="❌ Скасувати")],
        ],
        resize_keyboard=True,
    )


def days_inline_kb(prefix="day"):
    rows = [
        [
            InlineKeyboardButton(text="Понеділок", callback_data=f"{prefix}_0"),
            InlineKeyboardButton(text="Вівторок", callback_data=f"{prefix}_1"),
        ],
        [
            InlineKeyboardButton(text="Середа", callback_data=f"{prefix}_2"),
            InlineKeyboardButton(text="Четвер", callback_data=f"{prefix}_3"),
        ],
        [
            InlineKeyboardButton(text="П'ятниця", callback_data=f"{prefix}_4"),
            InlineKeyboardButton(text="Субота", callback_data=f"{prefix}_5"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def attendance_kb(pair_time: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Буду ✅", callback_data=f"att_present_{pair_time}"),
            InlineKeyboardButton(text="Запізнюся ⏱", callback_data=f"att_late_{pair_time}"),
            InlineKeyboardButton(text="Не буде ❌", callback_data=f"att_absent_{pair_time}"),
        ]
    ])


# === РОЗПІЗНАВАННЯ ФОТО РОЗКЛАДУ ЧЕРЕЗ GEMINI ШІ ===
async def parse_schedule_with_gemini(image_bytes: bytes, mime_type: str = "image/jpeg") -> list:
    if not GEMINI_API_KEY or "ВСТАВ_СЮДИ" in GEMINI_API_KEY:
        raise ValueError("Не вказано GEMINI_API_KEY у налаштуваннях бота!")

    b64_data = base64.b64encode(image_bytes).decode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"

    prompt = """
    Ти помічник старости університету. Проаналізуй це фото/скріншот розкладу пар і витягни всі заняття у вигляді строгого JSON-масиву об'єктів.
    Поля кожного об'єкта:
    - "day_idx": число від 0 до 5 (0 = Понеділок, 1 = Вівторок, 2 = Середа, 3 = Четвер, 4 = П'ятниця, 5 = Субота).
    - "week_type": "all" (якщо пара кожен тиждень), "num" (якщо чисельник / верхній / непарний тиждень), "den" (якщо знаменник / нижній / парний тиждень).
    - "pair_type": "Лек" (якщо лекція) або "Пр" (якщо практичне заняття, семінар або лабораторна). За замовчуванням "Пр".
    - "start_time": час початку у форматі "HH:MM" (наприклад "09:00").
    - "end_time": час кінця у форматі "HH:MM" (наприклад "10:20"), або "" якщо не вказано.
    - "subject": назва предмету українською мовою.
    - "teacher": ПІБ або ім'я викладача (якщо є на фото), інакше "".
    - "room": номер аудиторії або замітка (якщо є), інакше "".

    Поверни ТІЛЬКИ валідний JSON-масив без markdown-обгортки та зайвого тексту.
    """

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": b64_data}},
            ]
        }],
        "generationConfig": {"temperature": 0.1},
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=45) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                raise RuntimeError(f"Помилка Gemini API ({resp.status}): {err_text[:200]}")
            result = await resp.json()

    raw_text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.MULTILINE).strip()
    return json.loads(raw_text)


# === ПЛАНУВАЛЬНИК СПОВІЩЕНЬ ===
async def notify_pair(week_type: str, pair_type: str, start_time: str, end_time: str, subject: str, teacher: str, room: str, link: str):
    current_week = await get_current_week_type()
    if week_type != "all" and week_type != current_week:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM students")
        students = await cursor.fetchall()

    time_str = f"{start_time}–{end_time}" if end_time else start_time
    teacher_line = f"👨‍🏫 *Викладач:* {teacher}\n" if teacher else ""
    room_line = f"🚪 *Аудиторія:* {room}\n" if room else ""
    link_line = f"🔗 [Підключитися до пари]({link})\n" if link else ""
    week_note = f" • _{week_label(week_type)}_" if week_type != "all" else ""

    msg_text = (
        f"⏳ *Через 10 хвилин починається пара!*\n\n"
        f"🏷 `[{pair_type}]` *{subject}*{week_note}\n"
        f"⏰ *Час:* {time_str}\n"
        f"{teacher_line}"
        f"{room_line}"
        f"{link_line}\n"
        f"Позначте свій статус присутності:"
    )

    for s in students:
        try:
            await bot.send_message(
                chat_id=s[0],
                text=msg_text,
                reply_markup=attendance_kb(start_time),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:
            pass


async def reload_scheduler():
    scheduler.remove_all_jobs()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT day_idx, week_type, pair_type, start_time, end_time, subject, teacher, room, link FROM schedule"
        )
        rows = await cursor.fetchall()

    for day_idx, week_type, pair_type, start_time, end_time, subject, teacher, room, link in rows:
        try:
            hour, minute = map(int, start_time.split(":"))
            total_minutes = hour * 60 + minute - 10
            if total_minutes < 0:
                total_minutes += 24 * 60
            notif_h = total_minutes // 60
            notif_m = total_minutes % 60

            scheduler.add_job(
                notify_pair,
                "cron",
                day_of_week=day_idx,
                hour=notif_h,
                minute=notif_m,
                args=[week_type, pair_type, start_time, end_time, subject, teacher, room, link],
            )
        except Exception as e:
            logging.error(f"Помилка додавання задачі в планувальник: {e}")


# === СТАРТ, СКАСУВАННЯ, ПЕРЕМИКАННЯ ТИЖНЯ ТА ВІДНОВЛЕННЯ БЕКАПУ ===
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO students (user_id, full_name) VALUES (?, ?)",
            (message.from_user.id, message.from_user.full_name),
        )
        await db.commit()
    is_admin = (message.from_user.id == STAROSTA_ID)
    cur_week = week_label(await get_current_week_type())
    await message.answer(
        f"Привіт, {message.from_user.first_name}! 👋\n"
        f"Я бот-помічник нашої групи.\n\n"
        f"🗓 Зараз іде: *{cur_week}*",
        parse_mode="Markdown",
        reply_markup=main_kb(is_admin),
    )


@dp.message(F.text == "❌ Скасувати")
async def cancel_action(message: types.Message, state: FSMContext):
    await state.clear()
    is_admin = (message.from_user.id == STAROSTA_ID)
    await message.answer("Дію скасовано.", reply_markup=main_kb(is_admin))


@dp.message(F.document, F.from_user.id == STAROSTA_ID)
async def restore_db_from_backup(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return
    if message.document.file_name and message.document.file_name.endswith(".db"):
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, DB_PATH)
        await init_db()
        await reload_scheduler()
        await message.answer("✅ Базу даних успішно відновлено з файлу! Усі пари, ДЗ та матеріали на місці.")


@dp.message(F.text == "🔄 Перемкнути тиждень")
async def toggle_week_type(message: types.Message):
    if message.from_user.id != STAROSTA_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'week_offset'")
        row = await cursor.fetchone()
        current_offset = int(row[0]) if row else 0
        new_offset = 1 if current_offset == 0 else 0
        await db.execute("UPDATE settings SET value = ? WHERE key = 'week_offset'", (str(new_offset),))
        await db.commit()

    new_week_name = week_label(await get_current_week_type())
    await send_db_backup()
    await message.answer(
        f"✅ Тиждень перемкнуто!\nЗараз для всієї групи відображається: *{new_week_name}*",
        parse_mode="Markdown",
    )


# === ЗАВАНТАЖЕННЯ РОЗКЛАДУ ПО ФОТО (ШІ) ===
@dp.message(F.text == "📸 Завантажити фото розкладу")
async def ai_schedule_start(message: types.Message, state: FSMContext):
    if message.from_user.id != STAROSTA_ID:
        return
    await message.answer(
        "📸 Надішліть **фото або скріншот** вашого розкладу.\n\n"
        "ШІ автоматично розпізнає дні тижня, час, предмети, викладачів, аудиторії, тип пари (Лек/Пр) та чисельник/знаменник.",
        parse_mode="Markdown",
        reply_markup=cancel_kb(),
    )
    await state.set_state(AdminStates.waiting_for_schedule_photo)


@dp.message(AdminStates.waiting_for_schedule_photo, F.photo)
async def ai_schedule_process(message: types.Message, state: FSMContext):
    status_msg = await message.answer("⏳ Аналізую фото розкладу за допомогою ШІ, зачекайте кілька секунд...")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        image_bytes = downloaded.read()

        pairs = await parse_schedule_with_gemini(image_bytes)
        if not pairs:
            await status_msg.edit_text("⚠️ Не вдалося знайти пари на цьому зображенні. Спробуйте більш чітке фото.")
            return

        async with aiosqlite.connect(DB_PATH) as db:
            for p in pairs:
                day_idx = int(p.get("day_idx", 0))
                if not (0 <= day_idx <= 5):
                    continue
                w_type = p.get("week_type", "all")
                if w_type not in ("all", "num", "den"):
                    w_type = "all"
                p_type = p.get("pair_type", "Пр")
                s_time = str(p.get("start_time", "09:00")).strip()
                e_time = str(p.get("end_time", "")).strip()
                subj = str(p.get("subject", "Предмет")).strip()
                teacher = str(p.get("teacher", "")).strip()
                room = str(p.get("room", "")).strip()

                await db.execute(
                    """
                    INSERT INTO schedule (day_idx, week_type, pair_type, start_time, end_time, subject, teacher, room, link)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
                    """,
                    (day_idx, w_type, p_type, s_time, e_time, subj, teacher, room),
                )
            await db.commit()

        await reload_scheduler()
        await send_db_backup()
        await state.clear()
        await status_msg.delete()
        await message.answer(
            f"✅ Успішно розпізнано та додано **{len(pairs)}** пар(и) у розклад!\n"
            f"Натисніть **📅 Розклад**, щоб перевірити по днях.",
            parse_mode="Markdown",
            reply_markup=main_kb(is_admin=True),
        )
    except Exception as e:
        logging.error(f"AI parse error: {e}")
        await status_msg.edit_text(f"❌ Помилка при розпізнаванні: `{e}`", parse_mode="Markdown")


# === ПЕРЕГЛЯД РОЗКЛАДУ (В СТИЛІ СКРІНШОТІВ) ===
async def render_day_schedule(day_idx: int, forced_week: str = None):
    cur_week = forced_week if forced_week else await get_current_week_type()
    cur_week_name = week_label(cur_week)
    day_name = DAYS_UA[day_idx].upper()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, week_type, pair_type, start_time, end_time, subject, teacher, room, link
            FROM schedule
            WHERE day_idx = ? AND (week_type = 'all' OR week_type = ?)
            ORDER BY start_time
            """,
            (day_idx, cur_week),
        )
        pairs = await cursor.fetchall()

    other_week = "den" if cur_week == "num" else "num"
    other_week_label = "Знаменник" if cur_week == "num" else "Чисельник"

    kb_rows = [
        [
            InlineKeyboardButton(
                text=f"🟣 Зараз: {cur_week_name} (перемкнути на {other_week_label})",
                callback_data=f"viewweek_{day_idx}_{other_week}",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"• {DAYS_SHORT[i]} •" if i == day_idx else DAYS_SHORT[i],
                callback_data=f"viewday_{i}_{cur_week}",
            )
            for i in range(6)
        ],
    ]

    text = f"🗓 *{day_name}*  |  _{cur_week_name}_\n"
    text += "━━━━━━━━━━━━━━━━━━\n\n"

    if not pairs:
        text += "Пар немає! 🎉\n"
    else:
        for p_id, w_type, p_type, s_time, e_time, subj, teacher, room, link in pairs:
            time_range = f"{s_time}–{e_time}" if e_time else s_time
            week_badge = f"  `[{week_label(w_type)}]`" if w_type != "all" else ""
            text += f"`[{p_type}]` *{subj}*{week_badge}\n"
            text += f"🕒 {time_range}"
            if room:
                text += f"  •  🚪 {room}"
            text += "\n"
            if teacher:
                text += f"👤 _{teacher}_\n"
            if link:
                text += f"🔗 [Підключитися]({link})\n"
            text += "\n"
            kb_rows.append([InlineKeyboardButton(text=f"📝 ДЗ: {subj}", callback_data=f"hw_{subj}")])

    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@dp.message(F.text == "📅 Розклад")
async def show_schedule_today(message: types.Message):
    today_idx = datetime.now(KYIV_TZ).weekday()
    if today_idx > 5:
        today_idx = 0
    text, kb = await render_day_schedule(today_idx)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("viewday_"))
async def switch_schedule_day(call: types.CallbackQuery):
    _, day_str, week_code = call.data.split("_")
    text, kb = await render_day_schedule(int(day_str), forced_week=week_code)
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.answer()


@dp.callback_query(F.data.startswith("viewweek_"))
async def switch_schedule_week_view(call: types.CallbackQuery):
    _, day_str, week_code = call.data.split("_")
    text, kb = await render_day_schedule(int(day_str), forced_week=week_code)
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.answer()


# === ДОДАВАННЯ ТА ВИДАЛЕННЯ ПАР ВРУЧНУ (СТАРОСТА) ===
@dp.message(F.text == "➕ Додати пару")
async def add_pair_start(message: types.Message, state: FSMContext):
    if message.from_user.id != STAROSTA_ID:
        return
    await message.answer("Оберіть день тижня:", reply_markup=days_inline_kb(prefix="addday"))
    await state.set_state(AdminStates.add_pair_day)


@dp.callback_query(AdminStates.add_pair_day, F.data.startswith("addday_"))
async def add_pair_day_chosen(call: types.CallbackQuery, state: FSMContext):
    day_idx = int(call.data.split("_")[1])
    await state.update_data(day_idx=day_idx)
    await call.message.delete()
    await call.message.answer(
        f"Обрано: *{DAYS_UA[day_idx]}*\nОберіть тиждень:",
        parse_mode="Markdown",
        reply_markup=week_choice_kb(),
    )
    await state.set_state(AdminStates.add_pair_week)


@dp.message(AdminStates.add_pair_week)
async def add_pair_week_chosen(message: types.Message, state: FSMContext):
    mapping = {
        "🔁 Кожен тиждень": "all",
        "🔼 Тільки Чисельник": "num",
        "🔽 Тільки Знаменник": "den",
    }
    if message.text not in mapping:
        await message.answer("Оберіть варіант кнопкою нижче:", reply_markup=week_choice_kb())
        return
    await state.update_data(week_type=mapping[message.text])
    await message.answer("Оберіть тип заняття:", reply_markup=type_choice_kb())
    await state.set_state(AdminStates.add_pair_type)


@dp.message(AdminStates.add_pair_type)
async def add_pair_type_chosen(message: types.Message, state: FSMContext):
    if "Лек" in message.text:
        p_type = "Лек"
    elif "Пр" in message.text:
        p_type = "Пр"
    else:
        await message.answer("Оберіть кнопкою нижче:", reply_markup=type_choice_kb())
        return
    await state.update_data(pair_type=p_type)
    await message.answer(
        "Введіть час пари (наприклад `09:00-10:20` або просто `09:00`):",
        parse_mode="Markdown",
        reply_markup=cancel_kb(),
    )
    await state.set_state(AdminStates.add_pair_time)


@dp.message(AdminStates.add_pair_time)
async def add_pair_time_chosen(message: types.Message, state: FSMContext):
    raw = message.text.strip().replace("–", "-").replace("—", "-").replace(".", ":")
    parts = [p.strip() for p in raw.split("-")]
    try:
        h, m = map(int, parts[0].split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        start_t = f"{h:02d}:{m:02d}"
        end_t = ""
        if len(parts) > 1 and ":" in parts[1]:
            eh, em = map(int, parts[1].split(":"))
            end_t = f"{eh:02d}:{em:02d}"
    except Exception:
        await message.answer("⚠️ Невірний формат. Введіть, наприклад: `09:00-10:20`", parse_mode="Markdown")
        return

    await state.update_data(start_time=start_t, end_time=end_t)
    await message.answer("Введіть назву предмету:", reply_markup=cancel_kb())
    await state.set_state(AdminStates.add_pair_subject)


@dp.message(AdminStates.add_pair_subject)
async def add_pair_subject_chosen(message: types.Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await message.answer("Введіть ім'я/ПІБ викладача:", reply_markup=skip_kb("⏭ Пропустити викладача"))
    await state.set_state(AdminStates.add_pair_teacher)


@dp.message(AdminStates.add_pair_teacher)
async def add_pair_teacher_chosen(message: types.Message, state: FSMContext):
    teacher = "" if message.text == "⏭ Пропустити викладача" else message.text.strip()
    await state.update_data(teacher=teacher)
    await message.answer("Введіть номер аудиторії або замітку (наприклад `204` або `Сангiг`):", parse_mode="Markdown", reply_markup=skip_kb("⏭ Пропустити аудиторію"))
    await state.set_state(AdminStates.add_pair_room)


@dp.message(AdminStates.add_pair_room)
async def add_pair_room_chosen(message: types.Message, state: FSMContext):
    room = "" if message.text == "⏭ Пропустити аудиторію" else message.text.strip()
    await state.update_data(room=room)
    await message.answer("Надішліть посилання на пару (Zoom/Meet):", reply_markup=skip_kb("⏭ Без посилання"))
    await state.set_state(AdminStates.add_pair_link)


@dp.message(AdminStates.add_pair_link)
async def add_pair_finish(message: types.Message, state: FSMContext):
    link = "" if message.text == "⏭ Без посилання" else message.text.strip()
    data = await state.get_data()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO schedule (day_idx, week_type, pair_type, start_time, end_time, subject, teacher, room, link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["day_idx"],
                data["week_type"],
                data["pair_type"],
                data["start_time"],
                data["end_time"],
                data["subject"],
                data["teacher"],
                data["room"],
                link,
            ),
        )
        await db.commit()

    await reload_scheduler()
    await send_db_backup()
    await state.clear()
    await message.answer(
        f"✅ Пару *[{data['pair_type']}] {data['subject']}* збережено у розклад ({DAYS_UA[data['day_idx']]})!",
        parse_mode="Markdown",
        reply_markup=main_kb(is_admin=True),
    )


@dp.message(F.text == "🗑 Видалити пару")
async def del_pair_start(message: types.Message):
    if message.from_user.id != STAROSTA_ID:
        return
    await message.answer("Оберіть день, з якого видалити пару:", reply_markup=days_inline_kb(prefix="delday"))


@dp.callback_query(F.data.startswith("delday_"))
async def del_pair_list(call: types.CallbackQuery):
    if call.from_user.id != STAROSTA_ID:
        return
    day_idx = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, week_type, pair_type, start_time, subject FROM schedule WHERE day_idx = ? ORDER BY start_time",
            (day_idx,),
        )
        pairs = await cursor.fetchall()

    if not pairs:
        await call.message.edit_text(f"У день *{DAYS_UA[day_idx]}* пар немає.", parse_mode="Markdown")
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=f"❌ {s_time} [{p_type}] {subj} ({week_label(w_type)})",
                callback_data=f"delpair_{pair_id}",
            )
        ]
        for pair_id, w_type, p_type, s_time, subj in pairs
    ]
    buttons.append([InlineKeyboardButton(text="🗑 Очистити весь день", callback_data=f"clearday_{day_idx}")])
    await call.message.edit_text(
        f"Натисніть на пару для видалення ({DAYS_UA[day_idx]}):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("delpair_"))
async def del_pair_confirm(call: types.CallbackQuery):
    if call.from_user.id != STAROSTA_ID:
        return
    pair_id = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM schedule WHERE id = ?", (pair_id,))
        await db.commit()

    await reload_scheduler()
    await send_db_backup()
    await call.message.edit_text("✅ Пару видалено з розкладу.")


@dp.callback_query(F.data.startswith("clearday_"))
async def clear_day_confirm(call: types.CallbackQuery):
    if call.from_user.id != STAROSTA_ID:
        return
    day_idx = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM schedule WHERE day_idx = ?", (day_idx,))
        await db.commit()

    await reload_scheduler()
    await send_db_backup()
    await call.message.edit_text(f"✅ Усі пари на *{DAYS_UA[day_idx]}* видалено.", parse_mode="Markdown")


# === ДОМАШНЄ ЗАВДАННЯ ===
@dp.message(F.text == "📝 Домашнє завдання")
async def show_hw_subjects(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT subject FROM homework ORDER BY subject")
        subjects = await cursor.fetchall()

    if not subjects:
        await message.answer("Домашніх завдань поки що немає 🎉")
        return

    buttons = [[InlineKeyboardButton(text=f"📌 {s[0]}", callback_data=f"hw_{s[0]}")] for s in subjects]
    await message.answer("Оберіть предмет:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("hw_"))
async def show_hw_detail(call: types.CallbackQuery):
    subject = call.data.split("_", 1)[1]
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT task_text, file_id, file_type, updated_at FROM homework WHERE subject = ?",
            (subject,),
        )
        row = await cursor.fetchone()

    if not row:
        await call.answer("Домашнє завдання з цього предмету ще не додано!", show_alert=True)
        return

    task_text, file_id, file_type, updated_at = row
    msg = (
        f"📝 *Домашнє завдання: {subject}*\n"
        f"🕒 _Оновлено: {updated_at}_\n\n"
        f"{task_text}"
    )
    await call.message.answer(msg, parse_mode="Markdown")
    await call.answer()

    if file_id:
        if file_type == "photo":
            await bot.send_photo(chat_id=call.from_user.id, photo=file_id)
        else:
            await bot.send_document(chat_id=call.from_user.id, document=file_id)


@dp.message(F.text == "✏️ Додати ДЗ")
async def add_hw_start(message: types.Message, state: FSMContext):
    if message.from_user.id != STAROSTA_ID:
        return
    await message.answer(
        "Введіть назву предмету для ДЗ (наприклад: `Гістологія` або `Анатомія`):",
        parse_mode="Markdown",
        reply_markup=cancel_kb(),
    )
    await state.set_state(AdminStates.hw_subject)


@dp.message(AdminStates.hw_subject)
async def add_hw_subject(message: types.Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await message.answer("Напишіть текст домашнього завдання:", reply_markup=cancel_kb())
    await state.set_state(AdminStates.hw_text)


@dp.message(AdminStates.hw_text)
async def add_hw_text(message: types.Message, state: FSMContext):
    await state.update_data(task_text=message.text.strip())
    await message.answer(
        "Прикріпіть файл або фото до ДЗ (або натисніть «✅ Зберегти без файлу»):",
        reply_markup=skip_kb("✅ Зберегти без файлу"),
    )
    await state.set_state(AdminStates.hw_file)


@dp.message(AdminStates.hw_file)
async def add_hw_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    file_id = ""
    file_type = ""

    if message.text != "✅ Зберегти без файлу":
        if message.document:
            file_id = message.document.file_id
            file_type = "document"
        elif message.photo:
            file_id = message.photo[-1].file_id
            file_type = "photo"
        else:
            await message.answer("⚠️ Надішліть документ, фото або натисніть «✅ Зберегти без файлу».")
            return

    now_str = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO homework (subject, task_text, file_id, file_type, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (data["subject"], data["task_text"], file_id, file_type, now_str),
        )
        await db.commit()

    await send_db_backup()
    await state.clear()
    await message.answer(
        f"✅ Домашнє завдання з предмету *{data['subject']}* збережено!",
        parse_mode="Markdown",
        reply_markup=main_kb(is_admin=True),
    )


# === ВІДВІДУВАНІСТЬ ===
@dp.callback_query(F.data.startswith("att_"))
async def process_attendance(call: types.CallbackQuery):
    _, status_key, pair_time = call.data.split("_")
    status_map = {
        "present": "Буде ✅",
        "late": "Запізниться ⏱",
        "absent": "Не буде (НБ) ❌",
    }
    status_text = status_map.get(status_key, "Невідомо")
    today_str = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO attendance (date, pair_time, user_id, full_name, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (today_str, pair_time, call.from_user.id, call.from_user.full_name, status_text),
        )
        await db.commit()

    await call.answer(f"Ваш статус: {status_text}", show_alert=True)


@dp.message(F.text == "📊 Звіт відвідуваності")
async def attendance_report(message: types.Message):
    if message.from_user.id != STAROSTA_ID:
        return
    today_str = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT full_name, pair_time, status FROM attendance WHERE date = ? ORDER BY pair_time",
            (today_str,),
        )
        records = await cursor.fetchall()

    if not records:
        await message.answer("Сьогодні ще ніхто не відмічався.")
        return

    text = f"📊 *Звіт за {today_str}:*\n\n"
    for name, p_time, status in records:
        text += f"• {name} | {p_time} ➔ {status}\n"

    await message.answer(text, parse_mode="Markdown")


# === МАТЕРІАЛИ (ФАЙЛИ) ===
@dp.message(F.text == "📚 Матеріали")
async def show_subjects(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT DISTINCT subject FROM files")
        subjects = await cursor.fetchall()

    if not subjects:
        await message.answer("Файлів поки що немає 📁")
        return

    buttons = [[InlineKeyboardButton(text=s[0], callback_data=f"files_{s[0]}")] for s in subjects]
    await message.answer("Оберіть предмет:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("files_"))
async def send_subject_files(call: types.CallbackQuery):
    subject = call.data.split("_", 1)[1]
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT file_id, file_name FROM files WHERE subject = ?", (subject,))
        files = await cursor.fetchall()

    if not files:
        await call.message.answer("За цим предметом файлів не знайдено.")
        return

    await call.message.answer(f"📚 Матеріали з предмету *{subject}*:", parse_mode="Markdown")
    for f_id, f_name in files:
        await bot.send_document(chat_id=call.from_user.id, document=f_id, caption=f_name)


@dp.message(F.text == "➕ Додати файл")
async def admin_add_file(message: types.Message, state: FSMContext):
    if message.from_user.id != STAROSTA_ID:
        return
    await message.answer("Надішліть документ або методичку:", reply_markup=cancel_kb())
    await state.set_state(AdminStates.waiting_for_file)


@dp.message(AdminStates.waiting_for_file, F.document)
async def process_file_upload(message: types.Message, state: FSMContext):
    await state.update_data(file_id=message.document.file_id, file_name=message.document.file_name)
    await message.answer("Вкажіть назву предмету:", reply_markup=cancel_kb())
    await state.set_state(AdminStates.waiting_for_file_subject)


@dp.message(AdminStates.waiting_for_file_subject)
async def process_file_subject(message: types.Message, state: FSMContext):
    data = await state.get_data()
    subject = message.text.strip()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO files (file_id, file_name, subject) VALUES (?, ?, ?)",
            (data["file_id"], data["file_name"], subject),
        )
        await db.commit()

    await send_db_backup()
    await state.clear()
    await message.answer(
        f"✅ Файл '{data['file_name']}' додано до предмету '{subject}'!",
        reply_markup=main_kb(is_admin=True),
    )


# === ОГОЛОШЕННЯ ===
@dp.message(F.text == "📢 Оголошення")
async def admin_announce_start(message: types.Message, state: FSMContext):
    if message.from_user.id != STAROSTA_ID:
        return
    await message.answer("Введіть текст оголошення:", reply_markup=cancel_kb())
    await state.set_state(AdminStates.waiting_for_announcement)


@dp.message(AdminStates.waiting_for_announcement)
async def admin_announce_send(message: types.Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM students")
        students = await cursor.fetchall()

    count = 0
    for s in students:
        try:
            await bot.send_message(
                chat_id=s[0],
                text=f"📢 *ОГОЛОШЕННЯ ВІД СТАРОСТИ:*\n\n{message.text}",
                parse_mode="Markdown",
            )
            count += 1
        except Exception:
            pass

    await state.clear()
    await message.answer(
        f"✅ Оголошення надіслано {count} студентам.",
        reply_markup=main_kb(is_admin=True),
    )


# === ВЕБ-СЕРВЕР ДЛЯ RENDER ТА ЗАПУСК ===
async def ping(request):
    return web.Response(text="Bot is running!")


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await reload_scheduler()
    scheduler.start()

    app = web.Application()
    app.router.add_get("/", ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
