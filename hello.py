import asyncio
import json
import logging
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =====================================
# НАСТРОЙКИ
# =====================================

TOKEN = "8801443852:AAGiRFD2GvhThbROF-jgRXSWZ1HjWTIcqQs"

BASE_DIR = Path(__file__).resolve().parent
JSON_FILE = BASE_DIR / "tasks.json"
TIMEZONE = ZoneInfo("Asia/Almaty")

TASK_NAME, TASK_DEADLINE, TASK_INTERVAL = range(3)

tasks = {}
tasks_lock = threading.RLock()
timer_events = {}
next_task_id = 1

FACTS = [
    "🐙 У осьминога три сердца!",
    "🦈 Акулы появились раньше деревьев.",
    "🍌 Банан — ботаническая ягода.",
    "🐝 У пчёл пять глаз.",
    "🐌 Некоторые улитки могут спать несколько лет.",
]

JOKES = [
    "😂 Почему программист не любит природу? Там слишком много багов!",
    "💻 Компьютер устал и решил перезагрузиться.",
    "🐍 Почему Python пошёл к врачу? Проблемы с отступами!",
]


# =====================================
# JSON: СОХРАНЕНИЕ И ЗАГРУЗКА
# =====================================

def save_tasks():
    with tasks_lock:
        data = {
            "next_task_id": next_task_id,
            "tasks": tasks,
        }

        temp_file = JSON_FILE.with_suffix(".tmp")

        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        temp_file.replace(JSON_FILE)


def load_tasks():
    global tasks, next_task_id

    if not JSON_FILE.exists():
        tasks = {}
        next_task_id = 1
        return

    try:
        with JSON_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        tasks = data.get("tasks", {})
        saved_id = int(data.get("next_task_id", 1))

        max_id = max(
            (
                int(task["id"])
                for user_tasks in tasks.values()
                for task in user_tasks
            ),
            default=0,
        )

        next_task_id = max(saved_id, max_id + 1)

    except (OSError, ValueError, KeyError, TypeError):
        logging.exception("Ошибка чтения tasks.json")
        tasks = {}
        next_task_id = 1


# =====================================
# КЛАВИАТУРЫ
# =====================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📝 Добавить задачу"],
            ["📅 Мои задачи", "🗑 Удалить задачу"],
            ["🎲 Забавный факт", "😂 Joke"],
            ["🎮 Игры"],
        ],
        resize_keyboard=True,
    )


def games_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🎯 Угадай число", "⚽ Футбол"],
            ["⬅️ Назад"],
        ],
        resize_keyboard=True,
    )


def interval_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["1", "5", "60"],
            ["❌ Отмена"],
        ],
        resize_keyboard=True,
    )


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        [["❌ Отмена"]],
        resize_keyboard=True,
    )


# =====================================
# НАПОМИНАНИЯ ЧЕРЕЗ THREADING
# =====================================

def task_timer(task, application, cancel_event):
    """Фоновый поток отсчитывает время до дедлайна."""

    loop = application.bot_data["event_loop"]

    deadline = datetime.fromisoformat(task["deadline_at"])
    interval_seconds = task["interval"] * 60

    while not cancel_event.is_set():
        now = datetime.now(TIMEZONE)
        remaining = (deadline - now).total_seconds()

        if remaining <= 0:
            break

        wait_seconds = min(interval_seconds, remaining)

        if cancel_event.wait(wait_seconds):
            return

        now = datetime.now(TIMEZONE)
        remaining = (deadline - now).total_seconds()

        if remaining <= 0:
            break

        minutes_left = max(1, int((remaining + 59) // 60))

        future = asyncio.run_coroutine_threadsafe(
            application.bot.send_message(
                chat_id=task["chat_id"],
                text=(
                    "🔔 НАПОМИНАНИЕ!\n\n"
                    f"📝 {task['name']}\n"
                    f"⏳ До дедлайна примерно "
                    f"{minutes_left} мин."
                ),
            ),
            loop,
        )

        try:
            future.result(timeout=30)
        except Exception:
            logging.exception("Не удалось отправить напоминание")

    if cancel_event.is_set():
        return

    user_id = str(task["user_id"])
    task_id = task["id"]

    # Удаляем задачу после наступления дедлайна.
    with tasks_lock:
        user_tasks = tasks.get(user_id, [])

        tasks[user_id] = [
            item for item in user_tasks
            if item["id"] != task_id
        ]

        if not tasks[user_id]:
            tasks.pop(user_id, None)

        timer_events.pop(task_id, None)
        save_tasks()

    future = asyncio.run_coroutine_threadsafe(
        application.bot.send_message(
            chat_id=task["chat_id"],
            text=(
                "⏰ ДЕДЛАЙН НАСТУПИЛ!\n\n"
                f"📝 Задача: {task['name']}\n"
                "Проверь, успел ли ты её выполнить."
            ),
        ),
        loop,
    )

    try:
        future.result(timeout=30)
    except Exception:
        logging.exception("Не удалось отправить сообщение о дедлайне")


def start_timer(task, application):
    """Запускает отдельный поток для задачи."""

    task_id = task["id"]

    with tasks_lock:
        if task_id in timer_events:
            return

        event = threading.Event()
        timer_events[task_id] = event

    thread = threading.Thread(
        target=task_timer,
        args=(task, application, event),
        daemon=True,
        name=f"task-{task_id}",
    )

    thread.start()


async def post_init(application):
    """Восстанавливает задачи после запуска бота."""

    application.bot_data["event_loop"] = asyncio.get_running_loop()

    with tasks_lock:
        saved_tasks = [
            task
            for user_tasks in tasks.values()
            for task in user_tasks
        ]

    for task in saved_tasks:
        deadline = datetime.fromisoformat(task["deadline_at"])

        if deadline <= datetime.now(TIMEZONE):
            # Просроченные задачи сразу отмечаем уведомлением.
            with tasks_lock:
                user_id = str(task["user_id"])
                tasks[user_id] = [
                    item for item in tasks.get(user_id, [])
                    if item["id"] != task["id"]
                ]

                if not tasks[user_id]:
                    tasks.pop(user_id, None)

                save_tasks()

            await application.bot.send_message(
                chat_id=task["chat_id"],
                text=(
                    "⏰ Дедлайн уже наступил!\n\n"
                    f"📝 Задача: {task['name']}"
                ),
            )
        else:
            start_timer(task, application)


# =====================================
# СТАРТ
# =====================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("guess_game", None)
    context.user_data.pop("deleting", None)

    await update.message.reply_text(
        "👋 Привет! Я твой помощник.\n\n"
        "📝 Создавай задачи с дедлайнами.\n"
        "🔔 Настраивай напоминания.\n"
        "🎮 Играй в свободное время.\n\n"
        "Выбери действие:",
        reply_markup=main_keyboard(),
    )


# =====================================
# ДОБАВЛЕНИЕ ЗАДАЧИ
# =====================================

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 Напиши название задачи:",
        reply_markup=cancel_keyboard(),
    )
    return TASK_NAME


async def get_task_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)

    name = update.message.text.strip()

    if not name:
        await update.message.reply_text("Название не может быть пустым.")
        return TASK_NAME

    context.user_data["new_task_name"] = name

    await update.message.reply_text(
        "⏰ Через сколько минут наступит дедлайн?\n\n"
        "Например: 30\n"
        "Можно указать от 1 до 10080 минут."
    )

    return TASK_DEADLINE


async def get_task_deadline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.message.text == "❌ Отмена":
        return await cancel(update, context)

    try:
        minutes = int(update.message.text.strip())

        if not 1 <= minutes <= 10080:
            raise ValueError

    except ValueError:
        await update.message.reply_text(
            "❌ Введи целое число от 1 до 10080."
        )
        return TASK_DEADLINE

    context.user_data["deadline_minutes"] = minutes

    await update.message.reply_text(
        "🔔 Выбери интервал напоминаний в минутах:",
        reply_markup=interval_keyboard(),
    )

    return TASK_INTERVAL


async def get_task_interval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global next_task_id

    if update.message.text == "❌ Отмена":
        return await cancel(update, context)

    try:
        interval = int(update.message.text.strip())

        if interval not in (1, 5, 60):
            raise ValueError

    except ValueError:
        await update.message.reply_text(
            "Выбери интервал кнопкой: 1, 5 или 60."
        )
        return TASK_INTERVAL

    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    minutes = context.user_data["deadline_minutes"]

    deadline = datetime.now(TIMEZONE) + timedelta(minutes=minutes)

    with tasks_lock:
        task_id = next_task_id
        next_task_id += 1

        task = {
            "id": task_id,
            "user_id": int(user_id),
            "chat_id": chat_id,
            "name": context.user_data["new_task_name"],
            "interval": interval,
            "deadline_at": deadline.isoformat(),
        }

        tasks.setdefault(user_id, []).append(task)
        save_tasks()

    start_timer(task, context.application)

    context.user_data.pop("new_task_name", None)
    context.user_data.pop("deadline_minutes", None)

    await update.message.reply_text(
        "✅ Задача сохранена!\n\n"
        f"📝 {task['name']}\n"
        f"⏰ Дедлайн: {deadline:%H:%M %d.%m.%Y}\n"
        f"🔔 Напоминание каждые {interval} мин.",
        reply_markup=main_keyboard(),
    )

    return ConversationHandler.END


# =====================================
# СПИСОК ЗАДАЧ
# =====================================

async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    with tasks_lock:
        user_tasks = list(tasks.get(user_id, []))

    if not user_tasks:
        await update.message.reply_text(
            "📭 Активных задач нет.",
            reply_markup=main_keyboard(),
        )
        return

    lines = ["📅 Твои задачи:\n"]

    for index, task in enumerate(user_tasks, start=1):
        deadline = datetime.fromisoformat(task["deadline_at"])
        remaining = max(
            0,
            int((deadline - datetime.now(TIMEZONE)).total_seconds()),
        )
        minutes_left = (remaining + 59) // 60

        lines.append(
            f"{index}. 📝 {task['name']}\n"
            f"   ⏰ Дедлайн: {deadline:%H:%M %d.%m.%Y}\n"
            f"   🔔 Интервал: {task['interval']} мин.\n"
            f"   ⏳ Осталось: {minutes_left} мин.\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_keyboard(),
    )


# =====================================
# УДАЛЕНИЕ ЗАДАЧ
# =====================================

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    with tasks_lock:
        user_tasks = list(tasks.get(user_id, []))

    if not user_tasks:
        await update.message.reply_text(
            "📭 Активных задач нет.",
            reply_markup=main_keyboard(),
        )
        return

    lines = ["🗑 Напиши номер задачи для удаления:\n"]

    for index, task in enumerate(user_tasks, start=1):
        deadline = datetime.fromisoformat(task["deadline_at"])

        lines.append(
            f"{index}. {task['name']} — {deadline:%H:%M %d.%m}"
        )

    context.user_data["deleting"] = True

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=cancel_keyboard(),
    )


async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    try:
        index = int(update.message.text.strip()) - 1

        with tasks_lock:
            user_tasks = tasks.get(user_id, [])

            if index < 0 or index >= len(user_tasks):
                raise ValueError

            task = user_tasks.pop(index)

            if not user_tasks:
                tasks.pop(user_id, None)

            event = timer_events.pop(task["id"], None)
            save_tasks()

        if event:
            event.set()

        context.user_data["deleting"] = False

        await update.message.reply_text(
            f"🗑 Задача удалена: {task['name']}",
            reply_markup=main_keyboard(),
        )

    except ValueError:
        await update.message.reply_text(
            "❌ Неверный номер. Попробуй ещё раз."
        )


# =====================================
# ИГРА: УГАДАЙ ЧИСЛО
# =====================================

async def start_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["guess_game"] = True
    context.user_data["secret_number"] = random.randint(1, 100)
    context.user_data["guess_attempts"] = 0

    await update.message.reply_text(
        "🎯 Я загадал число от 1 до 100!\n\n"
        "🔥 Разница до 5 — очень близко.\n"
        "🟢 До 15 — близко.\n"
        "🟡 До 30 — далеко.\n"
        "🔴 Больше 30 — очень далеко.\n\n"
        "Пиши свой вариант!",
        reply_markup=games_keyboard(),
    )


async def guess_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        guess = int(update.message.text.strip())

        if not 1 <= guess <= 100:
            raise ValueError

    except ValueError:
        await update.message.reply_text("Введи число от 1 до 100.")
        return

    secret = context.user_data["secret_number"]
    context.user_data["guess_attempts"] += 1
    attempts = context.user_data["guess_attempts"]
    difference = abs(secret - guess)

    if guess == secret:
        context.user_data.pop("guess_game", None)
        context.user_data.pop("secret_number", None)
        context.user_data.pop("guess_attempts", None)

        await update.message.reply_text(
            f"🎉 Молодец! Ты угадал число {secret}!\n"
            f"Попыток: {attempts}",
            reply_markup=games_keyboard(),
        )
        return

    if difference <= 5:
        hint = "🔥 Очень близко!"
    elif difference <= 15:
        hint = "🟢 Близко!"
    elif difference <= 30:
        hint = "🟡 Далеко."
    else:
        hint = "🔴 Очень далеко!"

    direction = (
        "⬆️ Попробуй больше."
        if guess < secret
        else "⬇️ Попробуй меньше."
    )

    await update.message.reply_text(f"{hint}\n{direction}")


# =====================================
# ИГРА: ФУТБОЛЬНЫЙ БРОСОК
# =====================================

async def football(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Telegram сам показывает футбольную анимацию.
    await update.message.reply_dice(emoji="⚽")


# =====================================
# ФАКТЫ И ШУТКИ
# =====================================

async def funny_fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🎲 Забавный факт:\n\n{random.choice(FACTS)}",
        reply_markup=main_keyboard(),
    )


async def joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        random.choice(JOKES),
        reply_markup=main_keyboard(),
    )


# =====================================
# ОТМЕНА
# =====================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_task_name", None)
    context.user_data.pop("deadline_minutes", None)
    context.user_data.pop("deleting", None)

    await update.message.reply_text(
        "❌ Отменено.",
        reply_markup=main_keyboard(),
    )

    return ConversationHandler.END


# =====================================
# ОБРАБОТКА КНОПОК
# =====================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # Если пользователь играет в угадай число.
    if context.user_data.get("guess_game"):
        if text == "⬅️ Назад":
            context.user_data.pop("guess_game", None)
            context.user_data.pop("secret_number", None)
            context.user_data.pop("guess_attempts", None)

            await update.message.reply_text(
                "🎮 Меню игр:",
                reply_markup=games_keyboard(),
            )
            return

        if text == "🎯 Угадай число":
            await start_guess(update, context)
            return

        if text.isdigit():
            await guess_number(update, context)
            return

        await update.message.reply_text(
            "Введи число или нажми «⬅️ Назад»."
        )
        return

    # Если пользователь удаляет задачу.
    if context.user_data.get("deleting"):
        if text == "❌ Отмена":
            context.user_data["deleting"] = False

            await update.message.reply_text(
                "Удаление отменено.",
                reply_markup=main_keyboard(),
            )
            return

        await delete_task(update, context)
        return

    if text == "🎮 Игры":
        await update.message.reply_text(
            "🎮 Выбирай игру:",
            reply_markup=games_keyboard(),
        )

    elif text == "🎯 Угадай число":
        await start_guess(update, context)

    elif text == "⚽ Футбол":
        await football(update, context)

    elif text == "⬅️ Назад":
        await update.message.reply_text(
            "🏠 Главное меню:",
            reply_markup=main_keyboard(),
        )

    elif text == "📝 Добавить задачу":
        await add_task(update, context)

    elif text == "📅 Мои задачи":
        await show_tasks(update, context)

    elif text == "🗑 Удалить задачу":
        await delete_start(update, context)

    elif text == "🎲 Забавный факт":
        await funny_fact(update, context)

    elif text == "😂 Joke":
        await joke(update, context)


# =====================================
# ЗАПУСК БОТА
# =====================================

def main():
    logging.basicConfig(level=logging.INFO)

    load_tasks()

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    task_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_task),
            MessageHandler(
                filters.Regex("^📝 Добавить задачу$"),
                add_task,
            ),
        ],
        states={
            TASK_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_task_name,
                )
            ],
            TASK_DEADLINE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_task_deadline,
                )
            ],
            TASK_INTERVAL: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    get_task_interval,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(task_conversation)

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            button_handler,
        )
    )

    print("🤖 Бот запущен!")
    application.run_polling()


if __name__ == "__main__":
    main()