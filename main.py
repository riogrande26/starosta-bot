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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === НАЛАШТУВАННЯ ===
BOT_TOKEN = "8657527520:AAHOYz-jsgN4rrTA41Z-2NU2rCMO1paGvJw"
STAROSTA_ID = 741239201

DAYS_UA = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")


# === СТАНИ (FSM) ===
class AdminStates(StatesGroup):
    # Оголошення та файли
    waiting_for_announcement = State()
    waiting_for_file = State()
    waiting_for_file_subject = State()
    # Додавання пари
    add_pair_day = State()
    add_pair_time = State()
    add_pair_subject = State()
    add_pair_teacher = State()
    add_pair_link = State()
    # Домашнє завдання
    hw_subject = State()
    hw_text = State()
    hw_file = State()


# === БАЗА ДАНИХ ===
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_idx INTEGER,
                pair_time TEXT,
                subject TEXT,
                teacher TEXT,
                link TEXT
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
        await db.commit()


# === КЛАВІАТУРИ ===
def main_kb(is_admin=False):
    kb = [
        [KeyboardButton(text="📅 Розклад"), KeyboardButton(text="📝 Домашнє завдання")],
        [KeyboardButton(text="📚 Матеріали")],
    ]
    if is_admin:
        kb.append([KeyboardButton(text="➕ Додати пару"), KeyboardButton(text="🗑 Видалити пару")])
        kb.append([KeyboardButton(text="✏️ Додати ДЗ"), KeyboardButton(text="➕ Додати файл")])
        kb.append([KeyboardButton(text="📢 Оголошення"), KeyboardButton(text="📊 Звіт відвідуваності")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Скасувати")]],
        resize_keyboard=True
    )


def skip_kb(skip_text="⏭ Пропустити"):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=skip_text)], [KeyboardButton(text="❌ Скасувати")]],
        resize_keyboard=True
    )


def days_kb(prefix="day"):
    buttons = [[InlineKeyboardButton(text=DAYS_UA[i], callback_data=f"{prefix}_{i}")] for i in range(6)]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def attendance_kb(pair_time: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Буду ✅", callback_data=f"att_present_{pair_time}"),
            InlineKeyboardButton(text="Запізнюся ⏱", callback_data=f"att_late_{pair_time}"),
            InlineKeyboardButton(text="Не буде ❌", callback_data=f"att_absent_{pair_time}")
        ]
    ])


# === ПЛАНУВАЛЬНИК СПОВІЩЕНЬ ===
async def notify_pair(pair_time: str, subject: str, teacher: str, link: str):
    async with aiosqlite.connect("starosta.db") as db:
        cursor = await db.execute("SELECT user_id FROM students")
        students = await cursor.fetchall()

    teacher_line = f"👨‍🏫 *Викладач:* {teacher}\n" if teacher else ""
    link_line = f"🔗 [Підключитися до пари]({link})\n" if link else ""

    msg_text = (
        f"⏳ *Через 10 хвилин починається пара!*\n\n"
        f"📖 *Предмет:* {subject}\n"
        f"⏰ *Час:* {pair_time}\n"
        f"{teacher_line}"
        f"{link_line}\n"
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


async def reload_scheduler():
    scheduler.remove_all_jobs()
    async with aiosqlite.connect("starosta.db") as db:
        cursor = await db.execute("SELECT day_idx, pair_time, subject, teacher, link FROM schedule")
        rows = await cursor.fetchall()

    for day_idx, pair_time, subject, teacher, link in rows:
        try:
            hour, minute = map(int, pair_time.split(":"))
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
                args=[pair_time, subject, teacher, link]
            )
        except Exception as e:
            logging.error(f"Помилка додавання задачі в планувальник: {e}")


# === СТАРТ ТА СКАСУВАННЯ ===
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
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
        f"Тут можна дивитися розклад, домашні завдання, файли та відмічати відвідування.",
        reply_markup=main_kb(is_admin)
    )


@dp.message(F.text == "❌ Скасувати")
async def cancel_action(message: types.Message, state: FSMContext):
    await state.clear()
    is_admin = (message.from_user.id == STAROSTA_ID)
    await message.answer("Дію скасовано.", reply_markup=main_kb(is_admin))


# === ПЕРЕГЛЯД РОЗКЛАДУ ===
@dp.message(F.text == "📅 Розклад")
async def show_days(message: types.Message):
    await message.answer("Оберіть день тижня:", reply_markup=days_kb(prefix="day"))


@dp.callback_query(F.data.startswith("day_"))
async def show_schedule_day(call: types.CallbackQuery):
    day_idx = int(call.data.split("_")[1])
    day_name = DAYS_UA[day_idx]

    async with aiosqlite.connect("starosta.db") as db:
        cursor = await db.execute(
            "SELECT pair_time, subject, teacher, link FROM schedule WHERE day_idx = ? ORDER BY pair_time",
            (day_idx,)
        )
        pairs = await cursor.fetchall()

    if not pairs:
        await call.message.edit_text(f"📅 *{day_name}*\n\nПар немає! 🎉", parse_mode="Markdown")
        return

    text = f"📅 *{day_name}:*\n\n"
    for p_time, subj, teacher, link in pairs:
        text += f"⏰ *{p_time}* — {subj}\n"
        if teacher:
            text += f"👨‍🏫 Викладач: {teacher}\n"
        if link:
            text += f"🔗 [Посилання на пару]({link})\n"
        text += "\n"

    await call.message.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True)


# === УПРАВЛІННЯ РОЗКЛАДОМ (СТАРОСТА) ===
@dp.message(F.text == "➕ Додати пару")
async def add_pair_start(message: types.Message, state: FSMContext):
    if message.from_user.id != STAROSTA_ID:
        return
    await message.answer("Оберіть день тижня, на який додати пару:", reply_markup=days_kb(prefix="addday"))
    await state.set_state(AdminStates.add_pair_day)


@dp.callback_query(AdminStates.add_pair_day, F.data.startswith("addday_"))
async def add_pair_day_chosen(call: types.CallbackQuery, state: FSMContext):
    day_idx = int(call.data.split("_")[1])
    await state.update_data(day_idx=day_idx)
    await call.message.delete()
    await call.message.answer(
        f"Обрано: *{DAYS_UA[day_idx]}*\n\nВведіть час початку пари у форматі `ГГ:ХХ` (наприклад, `08:30` або `10:15`):",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
    )
    await state.set_state(AdminStates.add_pair_time)


@dp.message(AdminStates.add_pair_time)
async def add_pair_time_chosen(message: types.Message, state: FSMContext):
    time_text = message.text.strip().replace(".", ":")
    try:
        parts = time_text.split(":")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        formatted_time = f"{h:02d}:{m:02d}"
    except Exception:
        await message.answer("⚠️ Невірний формат часу. Введіть у форматі `08:30`:", parse_mode="Markdown")
        return

    await state.update_data(pair_time=formatted_time)
    await message.answer("Введіть назву предмету (наприклад: `Анатомія людини`):", parse_mode="Markdown", reply_markup=cancel_kb())
    await state.set_state(AdminStates.add_pair_subject)


@dp.message(AdminStates.add_pair_subject)
async def add_pair_subject_chosen(message: types.Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await message.answer(
        "Введіть ПІБ викладача (або натисніть «Пропустити»):",
        reply_markup=skip_kb("⏭ Пропустити викладача")
    )
    await state.set_state(AdminStates.add_pair_teacher)


@dp.message(AdminStates.add_pair_teacher)
async def add_pair_teacher_chosen(message: types.Message, state: FSMContext):
    teacher = "" if message.text == "⏭ Пропустити викладача" else message.text.strip()
    await state.update_data(teacher=teacher)
    await message.answer(
        "Надішліть посилання на Zoom / Google Meet (або натисніть «Без посилання», якщо пара очна):",
        reply_markup=skip_kb("⏭ Без посилання")
    )
    await state.set_state(AdminStates.add_pair_link)


@dp.message(AdminStates.add_pair_link)
async def add_pair_finish(message: types.Message, state: FSMContext):
    link = "" if message.text == "⏭ Без посилання" else message.text.strip()
    data = await state.get_data()

    async with aiosqlite.connect("starosta.db") as db:
        await db.execute(
            "INSERT INTO schedule (day_idx, pair_time, subject, teacher, link) VALUES (?, ?, ?, ?, ?)",
            (data["day_idx"], data["pair_time"], data["subject"], data["teacher"], link)
        )
        await db.commit()

    await reload_scheduler()
    await state.clear()
    await message.answer(
        f"✅ Пару *{data['subject']}* о *{data['pair_time']}* ({DAYS_UA[data['day_idx']]}) додано до розкладу!",
        parse_mode="Markdown",
        reply_markup=main_kb(is_admin=True)
    )


@dp.message(F.text == "🗑 Видалити пару")
async def del_pair_start(message: types.Message):
    if message.from_user.id != STAROSTA_ID:
        return
    await message.answer("Оберіть день, з якого потрібно видалити пару:", reply_markup=days_kb(prefix="delday"))


@dp.callback_query(F.data.startswith("delday_"))
async def del_pair_list(call: types.CallbackQuery):
    if call.from_user.id != STAROSTA_ID:
        return
    day_idx = int(call.data.split("_")[1])
    async with aiosqlite.connect("starosta.db") as db:
        cursor = await db.execute(
            "SELECT id, pair_time, subject FROM schedule WHERE day_idx = ? ORDER BY pair_time",
            (day_idx,)
        )
        pairs = await cursor.fetchall()

    if not pairs:
        await call.message.edit_text(f"У день *{DAYS_UA[day_idx]}* пар немає.", parse_mode="Markdown")
        return

    buttons = [
        [InlineKeyboardButton(text=f"❌ {p_time} — {subj}", callback_data=f"delpair_{pair_id}")]
        for pair_id, p_time, subj in pairs
    ]
    await call.message.edit_text(
        f"Натисніть на пару, яку потрібно видалити ({DAYS_UA[day_idx]}):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("delpair_"))
async def del_pair_confirm(call: types.CallbackQuery):
    if call.from_user.id != STAROSTA_ID:
        return
    pair_id = int(call.data.split("_")[1])
    async with aiosqlite.connect("starosta.db") as db:
        await db.execute("DELETE FROM schedule WHERE id = ?", (pair_id,))
        await db.commit()

    await reload_scheduler()
    await call.message.edit_text("✅ Пару видалено з розкладу.")


# === ДОМАШНЄ ЗАВДАННЯ ===
@dp.message(F.text == "📝 Домашнє завдання")
async def show_hw_subjects(message: types.Message):
    async with aiosqlite.connect("starosta.db") as db:
        cursor = await db.execute("SELECT subject FROM homework ORDER BY subject")
        subjects = await cursor.fetchall()

    if not subjects:
        await message.answer("Домашніх завдань поки що немає 🎉")
        return

    buttons = [[InlineKeyboardButton(text=f"📌 {s[0]}", callback_data=f"hw_{s[0]}")] for s in subjects]
    await message.answer("Оберіть предмет, щоб подивитися ДЗ:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("hw_"))
async def show_hw_detail(call: types.CallbackQuery):
    subject = call.data.split("_", 1)[1]
    async with aiosqlite.connect("starosta.db") as db:
        cursor = await db.execute(
            "SELECT task_text, file_id, file_type, updated_at FROM homework WHERE subject = ?",
            (subject,)
        )
        row = await cursor.fetchone()

    if not row:
        await call.message.answer("Завдання за цим предметом не знайдено.")
        return

    task_text, file_id, file_type, updated_at = row
    msg = (
        f"📝 *Домашнє завдання: {subject}*\n"
        f"🕒 _Оновлено: {updated_at}_\n\n"
        f"{task_text}"
    )
    await call.message.answer(msg, parse_mode="Markdown")

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
        "Введіть назву предмету, для якого додаєте домашнє завдання (наприклад: `Анатомія` або `Гістологія`):",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
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
        "Прикріпіть файл або фото до ДЗ (або натисніть «Зберегти без файлу»):",
        reply_markup=skip_kb("✅ Зберегти без файлу")
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

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    async with aiosqlite.connect("starosta.db") as db:
        await db.execute("""
            INSERT OR REPLACE INTO homework (subject, task_text, file_id, file_type, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (data["subject"], data["task_text"], file_id, file_type, now_str))
        await db.commit()

    await state.clear()
    await message.answer(
        f"✅ Домашнє завдання з предмету *{data['subject']}* збережено!",
        parse_mode="Markdown",
        reply_markup=main_kb(is_admin=True)
    )


# === ВІДВІДУВАНІСТЬ ===
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


# === МАТЕРІАЛИ (ФАЙЛИ) ===
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
    await message.answer("Надішліть документ або методичку:", reply_markup=cancel_kb())
    await state.set_state(AdminStates.waiting_for_file)


@dp.message(AdminStates.waiting_for_file, F.document)
async def process_file_upload(message: types.Message, state: FSMContext):
    await state.update_data(file_id=message.document.file_id, file_name=message.document.file_name)
    await message.answer("Вкажіть назву предмету (наприклад: Анатомія):", reply_markup=cancel_kb())
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

    await state.clear()
    await message.answer(
        f"✅ Файл '{data['file_name']}' додано до предмету '{subject}'!",
        reply_markup=main_kb(is_admin=True)
    )


# === ОГОЛОШЕННЯ ===
@dp.message(F.text == "📢 Оголошення")
async def admin_announce_start(message: types.Message, state: FSMContext):
    if message.from_user.id != STAROSTA_ID:
        return
    await message.answer("Введіть текст оголошення, яке побачить вся група:", reply_markup=cancel_kb())
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

    await state.clear()
    await message.answer(
        f"✅ Оголошення успішно надіслано {count} студентам.",
        reply_markup=main_kb(is_admin=True)
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
    app.router.add_get('/', ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
