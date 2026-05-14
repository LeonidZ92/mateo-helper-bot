import aiosqlite
import asyncio
import logging
import os
import pytz

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from datetime import datetime, timedelta
from dotenv import load_dotenv



# 🔐 Токен из переменной окружения

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = "users.db"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- FSM class ---

class ResetStates(StatesGroup):
    waiting_confirm = State() # проверка ответа для сброса streak

class NotifyStates(StatesGroup):
    choosing_days = State()   # пользователь выбирает дни
    choosing_time = State()   # пользователь вводит время

class ReportStates(StatesGroup):
    writing = State()  # пользователь вводит текст отчёта
    choosing_action = State()  # добавить или заменить

class OnboardingStates(StatesGroup):
    entering_streak_name = State() # пользователь вводит название серии

# --- DB helpers ---

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id  INTEGER PRIMARY KEY,
                streak   INTEGER DEFAULT 0,
                last_date TEXT
            )
        """)
        # миграция — добавляем колонки если их ещё нет
        for column, definition in [
            ("notify_days", "TEXT"),
            ("notify_time", "TEXT"),
            ("streak_name", "TEXT"),
            ("timezone", "TEXT")
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
                await db.commit()
            except aiosqlite.OperationalError:
                pass  # колонка уже есть — игнорируем ошибку

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER,
                date     TEXT,
                text     TEXT
            )
        """)
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT streak, last_date, notify_days, notify_time, streak_name, timezone FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()

async def upsert_user(user_id: int,
                      streak: int,
                      last_date: str | None,
                      notify_days: str | None = None,
                      notify_time: str | None = None,
                      streak_name: str | None = None,
                      timezone: str | None = None
                      ):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, streak, last_date, notify_days, notify_time, streak_name, timezone)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET streak = excluded.streak,
                                               last_date = excluded.last_date,
                                               notify_days = excluded.notify_days,
                                               notify_time = excluded.notify_time,
                                               streak_name = excluded.streak_name,
                                               timezone = excluded.timezone
        """, (user_id, streak, last_date, notify_days, notify_time, streak_name, timezone))
        await db.commit()


async def check_and_notify():
    now = datetime.now(pytz.utc)  # UTC время

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, notify_days, notify_time, timezone FROM users
            WHERE notify_days IS NOT NULL AND notify_time IS NOT NULL
        """) as cur:
            users = await cur.fetchall()

        for row in users:
            user_id, notify_days, notify_time, timezone = row

            # конвертируем UTC в timezone пользователя
            tz = pytz.timezone(timezone or "UTC")
            user_time = now.astimezone(tz)
            current_time = user_time.strftime("%H:%M")
            current_day = user_time.isoweekday()

            notify_days_list = [int(d) for d in notify_days.split(",")]
            if current_time == notify_time and current_day in notify_days_list:
                await bot.send_message(user_id, "Пришло время позаниматься! 💪")

async def scheduler():
    while True:
        await check_and_notify()
        await asyncio.sleep(60)

async def save_report(user_id: int, date: str, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reports (user_id, date, text) VALUES (?, ?, ?)",
            (user_id, date, text)
        )
        await db.commit()

async def get_reports(user_id: int, days: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT date, text FROM reports
            WHERE user_id = ?
            AND date >= date('now', ?)
            ORDER BY date DESC
        """, (user_id, f'-{days} days')) as cur:
            return await cur.fetchall()

async def get_today_report(user_id: int, date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM reports WHERE user_id = ? AND date = ?",
            (user_id, date)
        ) as cur:
            return await cur.fetchone()

async def replace_report(user_id: int, date: str, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reports SET text = ? WHERE user_id = ? AND date = ?",
            (text, user_id, date)
        )
        await db.commit()

# --- Keyboard ---

MAIN_KB = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="📊 Статистика")],
        [types.KeyboardButton(text="📝 Отчёт")],
        [types.KeyboardButton(text="🔔 Напоминания")],
    ],
    resize_keyboard=True,
)

STATS_KB = types.InlineKeyboardMarkup(
    inline_keyboard=[
        [types.InlineKeyboardButton(text="📈 Моя статистика", callback_data="stats")],
        [types.InlineKeyboardButton(text="🚫 Сброс статистики", callback_data="reset")],
    ]
)

REPORT_KB = types.InlineKeyboardMarkup(
    inline_keyboard=[
        [types.InlineKeyboardButton(text="✏️ Записать отчёт", callback_data="report_write")],
        [types.InlineKeyboardButton(text="📖 Посмотреть отчёт", callback_data="report_view")],
        [types.InlineKeyboardButton(text="🔄 Переименовать серию", callback_data="rename_streak")],
    ]
)

REPORT_ACTION_KB = types.InlineKeyboardMarkup(
    inline_keyboard=[
        [types.InlineKeyboardButton(text="➕ Добавить", callback_data="report_add")],
        [types.InlineKeyboardButton(text="🔄 Заменить", callback_data="report_replace")],
    ]
)

CONFIRM_KB = types.InlineKeyboardMarkup(
    inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Да", callback_data="reset_confirm"),
            types.InlineKeyboardButton(text="❌ Нет", callback_data="reset_cancel"),
        ]
    ]
)

NOTIFY_MANAGE_KB = types.InlineKeyboardMarkup(
    inline_keyboard=[
        [types.InlineKeyboardButton(text="✏️ Изменить", callback_data="notify_edit")],
        [types.InlineKeyboardButton(text="🗑 Удалить", callback_data="notify_delete")],
    ]
)

DAYS_KB = types.InlineKeyboardMarkup(
    inline_keyboard=[
        [
            types.InlineKeyboardButton(text="Пн", callback_data="day_1"),
            types.InlineKeyboardButton(text="Вт", callback_data="day_2"),
            types.InlineKeyboardButton(text="Ср", callback_data="day_3")
        ],
        [
            types.InlineKeyboardButton(text="Чт", callback_data="day_4"),
            types.InlineKeyboardButton(text="Пт", callback_data="day_5")
        ],
        [
            types.InlineKeyboardButton(text="Сб", callback_data="day_6"),
            types.InlineKeyboardButton(text="Вс", callback_data="day_7")
        ],
        [
            types.InlineKeyboardButton(text="Готово ✅", callback_data="days_done")
        ]
    ]
)

PERIOD_KB = types.InlineKeyboardMarkup(
    inline_keyboard=[
        [types.InlineKeyboardButton(text="📅 Сегодня", callback_data="period_1")],
        [types.InlineKeyboardButton(text="📅 7 дней", callback_data="period_7")],
        [types.InlineKeyboardButton(text="📅 30 дней", callback_data="period_30")],
    ]
)

GEO_KB = types.ReplyKeyboardMarkup(
    keyboard=[[types.KeyboardButton(text="📍 Отправить геолокацию", request_location=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# --- Dictionary ---
DAY_NAMES = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}

# --- Handlers ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user:
        await upsert_user(user_id, streak=0, last_date=None)

    await message.answer(
        "Привет! Я помогу тебе отслеживать рабочие привычки 👋\n\n"
        "📊 *Статистика* — текущая серия дней твоего проекта\n"
        "📝 *Отчёт* — записывай что сделал, смотри историю\n"
        "🔔 *Напоминания* — выбери дни и время уведомлений\n\n"
        "Начнём? 👇",
        reply_markup=MAIN_KB,
        parse_mode="Markdown"
    )

@dp.message(lambda m: m.text == "📊 Статистика")
async def menu_stats(message: types.Message):
    await message.answer("Выбери действие:", reply_markup=STATS_KB)

@dp.message(lambda m: m.text == "📝 Отчёт")
async def menu_report(message: types.Message):
    await message.answer("Выбери действие:", reply_markup=REPORT_KB)

@dp.message(lambda m: m.text == "🔔 Напоминания")
async def notify_button(message: types.Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    notify_days = user[2] if user else None
    notify_time = user[3] if user else None
    timezone = user[5] if user else None

    if not timezone:
        await message.answer(
            "Чтобы уведомления приходили в правильное время,\n"
            "мне нужно узнать твой часовой пояс 🌍\n\n"
            "Нажми кнопку ниже — я определю его автоматически\n"
            "по твоей геолокации. Данные используются только\n"
            "для настройки времени и нигде не сохраняются.",
            reply_markup=GEO_KB
        )
        return
    if notify_days and notify_time:
        days_names = [DAY_NAMES[int(d)] for d in notify_days.split(",")]
        days_str = ", ".join(days_names)
        await message.answer(
            f"🔔 Текущее расписание:\n\n📅 Дни: {days_str}\n\n⏰ Время: {notify_time}",
            reply_markup=NOTIFY_MANAGE_KB
        )
    else:
        await state.set_state(NotifyStates.choosing_days)
        await message.answer("Выбери дни для уведомлений:", reply_markup=DAYS_KB)

@dp.callback_query(lambda c: c.data == "notify_edit")
async def notify_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(NotifyStates.choosing_days)
    await callback.message.answer("Выбери дни для уведомлений:", reply_markup=DAYS_KB)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "notify_delete")
async def notify_delete(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user = await get_user(user_id)
    streak = user[0] if user else 0
    last_date = user[1] if user else None
    streak_name = user[4] if user and user[4] else "Серия"
    await upsert_user(user_id, streak, last_date, None, None, streak_name)
    await callback.message.answer("🗑 Расписание уведомлений удалено", reply_markup=MAIN_KB)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "stats")
async def stats(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    streak = user[0] if user else 0
    streak_name = user[4] if user and user[4] else "Серия"
    await callback.message.answer(f"🔥 {streak_name}: {streak} дней")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "reset")
async def reset(callback: types.CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    streak = user[0] if user else 0
    await callback.message.answer(f"Вы точно хотите сбросить свою серию: {streak} дней?",
                                  reply_markup=CONFIRM_KB)
    await callback.answer()
    await state.set_state(ResetStates.waiting_confirm)

@dp.callback_query(lambda c: c.data in ["reset_confirm", "reset_cancel"])
async def reset_confirm(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user = await get_user(user_id)
    streak = user[0]
    if callback.data == "reset_confirm":
        await upsert_user(user_id, 0, None,
                          notify_days=user[2], notify_time=user[3], streak_name=user[4], timezone=user[5])
        await callback.message.answer("Серия сброшена. Начинай заново! 💪", reply_markup=MAIN_KB)
    else:
        await callback.message.answer(f"Отмена сброса 😌 Твоя серия: {streak} дней",
                                      reply_markup=MAIN_KB)
    await callback.answer()
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("day_"))
async def handle_day(callback: types.CallbackQuery, state: FSMContext):
    # 1. достань текущий список выбранных дней из state
    data = await state.get_data()
    days = data.get("selected_days", [])
    # 2. добавь или убери день (если уже есть — убери, если нет — добавь)
    day = int(callback.data.split("_")[1])  # из "day_3" получаем 3

    if day in days:
        days.remove(day)  # уже выбран — убираем
    else:
        days.append(day)  # не выбран — добавляем
    # 3. сохрани обратно в state
    await state.update_data(selected_days=days)
    days_names = [DAY_NAMES[d] for d in days]
    # для каждого d из списка days → берём DAY_NAMES[d]
    # результат: ["Пн", "Ср", "Пт"]

    # 4. ответь пользователю что выбрано (callback.answer("..."))
    result = ", ".join(days_names)
    await callback.answer(f"Выбрано: {result}")

@dp.callback_query(lambda c: c.data == "days_done")
async def handle_days_done(callback: types.CallbackQuery, state: FSMContext):
    # 1. достань список дней из state
    data = await state.get_data()
    days = data.get("selected_days", [])
    # 2. если пустой — callback.answer("Выбери хотя бы один день!")
    if not days:
        await callback.answer("Выбери хотя бы один день!")
    # 3. если не пустой:
    #    - отправь сообщение "Введи время в формате ЧЧ:ММ (например 20:00)"
    #    - переключи состояние на NotifyStates.choosing_time
    else:
        await callback.message.answer("Введи время в формате ЧЧ:ММ (например 20:00)")
        await callback.answer()
        await state.set_state(NotifyStates.choosing_time)

@dp.message(NotifyStates.choosing_time)
async def handle_time(message: types.Message, state: FSMContext):
    # 1. достань days из state
    data = await state.get_data()
    days = data.get("selected_days", [])
    # 2. проверь формат времени — должно быть "ЧЧ:ММ"
    #    подсказка: попробуй datetime.strptime(message.text, "%H:%M")
    #    если формат неверный — strptime бросит исключение ValueError
    try:
        time_obj = datetime.strptime(message.text, "%H:%M")
    except ValueError:
        await message.answer("Неверный формат. Введи время как 20:00")
        return
    # 3. сохрани days и время в БД через upsert_user (нужно будет его доработать)
    user_id = message.from_user.id
    user = await get_user(user_id)
    streak = user[0] if user else 0
    last_date = user[1] if user else None
    notify_days = ",".join(str(d) for d in days)
    notify_time = message.text
    streak_name = user[4] if user and user[4] else "Серия"
    timezone = user[5] if user else None
    await upsert_user(user_id, streak, last_date, notify_days, notify_time, streak_name, timezone)
    # 4. ответь пользователю что настройки сохранены
    days_names = [DAY_NAMES[d] for d in days]
    days_str = ", ".join(days_names)
    await message.answer(f"✅ Расписание сохранено!\n\n📅 Дни: {days_str}\n\n⏰ Время: {notify_time}", reply_markup=MAIN_KB)
    # 5. state.clear()
    await state.clear()

@dp.callback_query(lambda c: c.data == "report_write")
async def report_write(callback: types.CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    streak_name = user[4] if user else None

    if not streak_name:
        await callback.message.answer("Как назовём твою серию? Например: 'Читаю каждый день'")
        await callback.answer()
        await state.set_state(OnboardingStates.entering_streak_name)
    else:
        await callback.message.answer("Напиши, что получилось выполнить сегодня?")
        await callback.answer()
        await state.set_state(ReportStates.writing)

@dp.message(ReportStates.writing)
async def report_save(message: types.Message, state: FSMContext):
    # 1. сохранить текст в reports через save_report
    user_id = message.from_user.id
    today = datetime.now().date()
    existing = await get_today_report(user_id, str(today))
    data = await state.get_data()
    action = data.get("report_action")

    if existing and action is None:
        # первый раз — спрашиваем что делать
        await message.answer("Ты уже писал отчёт сегодня. Что хочешь сделать?",
                             reply_markup=REPORT_ACTION_KB)
        await state.update_data(report_text=message.text)  # сохраняем текст
        await state.set_state(ReportStates.choosing_action)
        return
    elif action == "report_replace":
        await replace_report(user_id, str(today), message.text)
    elif action == "report_add":
        await save_report(user_id, str(today), message.text)
    else:
        await save_report(user_id, str(today), message.text)  # первая запись за день
    # 2. обновить streak (логика из старого worked)
    if action in ["report_replace", "report_add"]:
        await message.answer("📝 Отчёт обновлён!")
        await state.clear()
        return
    user = await get_user(user_id)
    if not user:
        await upsert_user(user_id, 0, None)
        user = (0, None, None, None, None, None)  # ✅ шесть значений

    streak, last_date, notify_days, notify_time, streak_name, timezone = user

    streak += 1
    # 3. ответить пользователю
    await upsert_user(user_id, streak, str(today),
                      notify_days=notify_days, notify_time=notify_time, streak_name=streak_name, timezone=timezone)
    await message.answer(f"📝 Отчёт сохранён! 🔥 {streak_name}: {streak} дней!")
    # 4. state.clear()
    await state.clear()

@dp.callback_query(lambda c: c.data in ["report_add", "report_replace"])
async def report_action(callback: types.CallbackQuery, state: FSMContext):
    # 1. сохрани выбор пользователя в state
    await state.update_data(report_action=callback.data)
    # 2. попроси написать текст
    await callback.message.answer("Напиши текст отчёта:")
    await callback.answer()
    # 3. переключи состояние обратно на writing
    await state.set_state(ReportStates.writing)

@dp.callback_query(lambda c: c.data == "report_view")
async def report_view(callback: types.CallbackQuery):
    # показать PERIOD_KB с вопросом "За какой период?"
    await callback.message.answer("За какой период?", reply_markup=PERIOD_KB)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("period_"))
async def report_period(callback: types.CallbackQuery):
    # 1. достать число дней из callback.data ("period_7" → 7)
    days = int(callback.data.split("_")[1])
    # 2. получить отчёты через get_reports
    user_id = callback.from_user.id
    reports = await get_reports(user_id, days)
    # 3. если пусто — сообщить что записей нет
    if not reports:
        await callback.message.answer("За этот период записей нет 📭")
    # 4. если есть — отформатировать и отправить
    else:
        lines = []
        for date, text in reports:
            lines.append(f"📅 {date}\n{text}")
        result = "\n\n".join(lines)
        await callback.message.answer(result)
    await callback.answer()

@dp.message(OnboardingStates.entering_streak_name)
async def save_streak_name(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = await get_user(user_id)
    streak_name = message.text
    streak = user[0] if user else 0
    last_date = user[1] if user else None
    notify_days = user[2] if user else None
    notify_time = user[3] if user else None
    timezone = user[5] if user else None
    await upsert_user(user_id, streak, last_date,
                      notify_days=notify_days, notify_time=notify_time,
                      streak_name=streak_name, timezone=timezone)

    data = await state.get_data()
    if data.get("rename_only"):
        await message.answer(f"✅ Серия переименована: {streak_name}!", reply_markup=MAIN_KB)
        await state.clear()
    else:
        await message.answer("Напиши, что получилось выполнить сегодня?")
        await state.set_state(ReportStates.writing)

@dp.callback_query(lambda c: c.data == "rename_streak")
async def rename_streak(callback: types.CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    streak_name = user[4] if user and user[4] else "Серия"
    await callback.message.answer(f"Текущее название: {streak_name}\n\nКак переименуем?")
    await callback.answer()
    await state.update_data(rename_only=True)  # ← флаг что это переименование
    await state.set_state(OnboardingStates.entering_streak_name)


@dp.message(lambda m: m.location is not None)
async def handle_location(message: types.Message, state: FSMContext):
    from timezonefinder import TimezoneFinder

    lat = message.location.latitude
    lon = message.location.longitude

    tf = TimezoneFinder()
    timezone = tf.timezone_at(lat=lat, lng=lon)  # например "Europe/Moscow"

    # сохрани timezone в БД
    user_id = message.from_user.id
    user = await get_user(user_id)
    # достань текущие значения и сохрани с timezone
    streak = user[0] if user else 0
    last_date = user[1] if user else None
    notify_days = user[2] if user else None
    notify_time = user[3] if user else None
    streak_name = user[4] if user and user[4] else "Серия"
    await upsert_user(user_id, streak, last_date, notify_days, notify_time, streak_name, timezone)

    await message.answer(
        f"✅ Часовой пояс определён: {timezone}\n\nТеперь выбери дни для уведомлений:",
        reply_markup=DAYS_KB
    )
    await state.set_state(NotifyStates.choosing_days)

# --- Entry point ---

async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())