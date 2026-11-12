import asyncio
import logging
import os
from datetime import datetime
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === НАЛАШТУВАННЯ ===
BOT_TOKEN = "8657527520:AAHOYz-jsgN4rrTA41Z-2NU2rCMO1paGvJw"

STAROSTA_ID = 741239201


# Розклад пар: 0 - Понеділок, 1 - Вівторок ... 4 - П'ятниця
SCHEDULE = {
    0: [
        {"time": "08:30", "subject": "Анатомія людини", "link": "https://zoom.us/j/123"},
        {"time": "10:15", "subject": "Медична біологія", "link": "https://meet.google.com/abc"},
    ],
    1: [
        {"time": "08:30", "subject": "Гістологія", "link": "https://zoom.us/j/123"},
        {"time": "12:00", "subject": "Латинська мова", "link": "https://meet.google.com/abc"},
    ],
    2: [
        {"time": "10:15", "subject": "Медична хімія", "link": "https://zoom.us/j/123"},
    ],
    3: [
        {"time": "08:30", "subject": "Анатомія (Практика)", "link": "https://zoom.us/j/123"},
    ],
    4: [
        {"time": "10:15", "subject": "Фізіологія", "link": "https://zoom.us/j/123"},
    ]
}

DAYS_UA = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")

class AdminStates(StatesGroup):
    waiting_for_announcement = State()
    waiting_for_file = State()
    waiting_for_file_subject = State()

async def init_db():
    async with aiosqlite.connect("starosta.db") as db:
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
        await db.commit()

def main_kb(is_admin=False):
    kb = [
        [types.KeyboardButton(text="📅 Розклад"), types.KeyboardButton(text="📚 Матеріали")],
    ]
    if is_admin:
        kb.append([types.KeyboardButton(text="📢 Оголошення"), types.KeyboardButton(text="➕ Додати файл")])
        kb.append([types.KeyboardButton(text="📊 Звіт відвідуваності")])
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def days_kb():
    buttons = [[InlineKeyboardButton(text=DAYS_UA[i], callback_data=f"day_{i}")] for i in range(5)]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def attendance_kb(pair_time: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Буду ✅", callback_data=f"att_present_{pair_time}"),
            InlineKeyboardButton(text="Запізнюся ⏱", callback_data=f"att_late_{pair_time}"),
            InlineKeyboardButton(text="Не буде ❌", callback_data=f"att_absent_{pair_time}")
        ]
    ])

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    async with aiosqlite.connect("starosta.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO students (user_id, full_name) VALUES (?, ?)",
            (message.from_user.id, message.from_user.full_name)
        )
        await db.commit()
    is_admin = (message.from_user.id == STAROSTA_ID)
    await message.answer(
        f"Привіт, {message.from_user.first_name}! 👋\n"
        f"Я бот-помічник нашої групи.\n"
        f"Тут можна дивитися розклад, завантажувати файли та відмічати відвідування.",
        reply_markup=main_kb(is_admin)
    )

@dp.message(F.text == "📅 Розклад")
async def show_days(message: types.Message):
    await message.answer("Оберіть день тижня:", reply_markup=days_kb())

@dp.callback_query(F.data.startswith("day_"))
async def show_schedule_day(call: types.CallbackQuery):
    day_idx = int(call.data.split("_")[1])
    day_name = DAYS_UA[day_idx]
    pairs = SCHEDULE.get(day_idx, [])
    
    if not pairs:
        await call.message.edit_text(f"📅 *{day_name}*\n\nПар немає! 🎉", parse_mode="Markdown")
        return

    text = f"📅 *{day_name}:*\n\n"
    for p in pairs:
        text += f"⏰ *{p['time']}* — {p['subject']}\n🔗 [Посилання на пару]({p['link']})\n\n"
    
    await call.message.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True)

@dp.callback_query(F.data.startswith("att_"))
async def process_attendance(call: types.CallbackQuery):
    _, status_key, pair_time = call.data.split("_")
    status_map = {
        "present": "Буде ✅",
        "late": "Запізниться ⏱",
        "absent": "Не буде (НБ) ❌"
    }
    status_text = status_map.get(status_key, "Невідомо")
    today_str = datetime.now().strftime("%Y-%m-%d")

    async with aiosqlite.connect("starosta.db") as db:
        await db.execute("""
            INSERT OR REPLACE INTO attendance (date, pair_time, user_id, full_name, status)
            VALUES (?, ?, ?, ?, ?)
        """, (today_str, pair_time, call.from_user.id, call.from_user.full_name, status_text))
        await db.commit()

    await call.answer(f"Ваш статус: {status_text}", show_alert=True)

@dp.message(F.text == "📚 Матеріали")
async def show_subjects(message: types.Message):
    async with aiosqlite.connect("starosta.db") as db:
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
    async with aiosqlite.connect("starosta.db") as db:
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
    await message.answer("Надішліть документ або методичку:")
    await state.set_state(AdminStates.waiting_for_file)

@dp.message(AdminStates.waiting_for_file, F.document)
async def process_file_upload(message: types.Message, state: FSMContext):
    await state.update_data(file_id=message.document.file_id, file_name=message.document.file_name)
    await message.answer("Вкажіть назву предмету (наприклад: Анатомія):")
    await state.set_state(AdminStates.waiting_for_file_subject)

@dp.message(AdminStates.waiting_for_file_subject)
async def process_file_subject(message: types.Message, state: FSMContext):
    data = await state.get_data()
    subject = message.text.strip()
    
    async with aiosqlite.connect("starosta.db") as db:
        await db.execute(
            "INSERT INTO files (file_id, file_name, subject) VALUES (?, ?, ?)",
            (data["file_id"], data["file_name"], subject)
        )
        await db.commit()
    
    await message.answer(f"✅ Файл '{data['file_name']}' додано до предмету '{subject}'!")
    await state.clear()

@dp.message(F.text == "📢 Оголошення")
async def admin_announce_start(message: types.Message, state: FSMContext):
    if message.from_user.id != STAROSTA_ID:
        return
    await message.answer("Введіть текст оголошення, яке побачить вся група:")
    await state.set_state(AdminStates.waiting_for_announcement)

@dp.message(AdminStates.waiting_for_announcement)
async def admin_announce_send(message: types.Message, state: FSMContext):
    async with aiosqlite.connect("starosta.db") as db:
        cursor = await db.execute("SELECT user_id FROM students")
        students = await cursor.fetchall()

    count = 0
    for s in students:
        try:
            await bot.send_message(
                chat_id=s[0],
                text=f"📢 *ОГОЛОШЕННЯ ВІД СТАРОСТИ:*\n\n{message.text}",
                parse_mode="Markdown"
            )
            count += 1
        except Exception:
            pass

    await message.answer(f"✅ Оголошення успішно надіслано {count} студентам.")
    await state.clear()

@dp.message(F.text == "📊 Звіт відвідуваності")
async def attendance_report(message: types.Message):
    if message.from_user.id != STAROSTA_ID:
        return
    today_str = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect("starosta.db") as db:
        cursor = await db.execute(
            "SELECT full_name, pair_time, status FROM attendance WHERE date = ? ORDER BY pair_time",
            (today_str,)
        )
        records = await cursor.fetchall()

    if not records:
        await message.answer("Сьогодні ще ніхто не відмічався.")
        return

    text = f"📊 *Звіт за {today_str}:*\n\n"
    for name, p_time, status in records:
        text += f"• {name} | {p_time} ➔ {status}\n"

    await message.answer(text, parse_mode="Markdown")

async def notify_pair(pair_time: str, subject: str, link: str):
    async with aiosqlite.connect("starosta.db") as db:
        cursor = await db.execute("SELECT user_id FROM students")
        students = await cursor.fetchall()

    msg_text = (
        f"⏳ *Через 10 хвилин починається пара!*\n\n"
        f"📖 *Предмет:* {subject}\n"
        f"⏰ *Час:* {pair_time}\n"
        f"🔗 [Підключитися до пари]({link})\n\n"
        f"Позначте свій статус присутності:"
    )

    for s in students:
        try:
            await bot.send_message(
                chat_id=s[0],
                text=msg_text,
                reply_markup=attendance_kb(pair_time),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        except Exception:
            pass

def setup_scheduler():
    for day_idx, pairs in SCHEDULE.items():
        for p in pairs:
            hour, minute = map(int, p["time"].split(":"))
            total_minutes = hour * 60 + minute - 10
            notif_h = total_minutes // 60
            notif_m = total_minutes % 60
            
            scheduler.add_job(
                notify_pair,
                "cron",
                day_of_week=day_idx,
                hour=notif_h,
                minute=notif_m,
                args=[p["time"], p["subject"], p["link"]]
            )

async def ping(request):
    return web.Response(text="Bot is running!")

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    setup_scheduler()
    scheduler.start()

    app = web.Application()
    app.router.add_get('/', ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
