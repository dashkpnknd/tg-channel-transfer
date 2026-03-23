import asyncio
import logging
import os
from typing import Dict

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram import Client, errors

import copy_worker
import database_handler as db
import local_config as config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

auth_sessions: Dict[int, dict] = {}


class Form(StatesGroup):
    phone = State()
    code = State()
    password = State()
    source = State()
    target = State()
    replace_old = State()
    replace_new = State()


def ensure_sessions_dir() -> None:
    os.makedirs("sessions", exist_ok=True)


def get_session_path(user_id: int) -> str:
    return f"sessions/user_{user_id}"


def get_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    session_exists = os.path.exists(f"{get_session_path(user_id)}.session")
    status_emoji = "✅" if session_exists else "❌"

    buttons = [
        [InlineKeyboardButton(text=f"{status_emoji} 1. Подключить аккаунт", callback_data="auth_start")],
        [
            InlineKeyboardButton(text="📥 2. Откуда (Источник)", callback_data="set_source"),
            InlineKeyboardButton(text="📤 3. Куда (Цель)", callback_data="set_target"),
        ],
        [
            InlineKeyboardButton(text="📝 Добавить замену", callback_data="add_replace"),
            InlineKeyboardButton(text="📋 Список замен", callback_data="list_replaces"),
        ],
        [InlineKeyboardButton(text="🗑 Очистить замены", callback_data="clear_replaces")],
        [InlineKeyboardButton(text="▶️ СКОПИРОВАТЬ ПОСТЫ", callback_data="start_worker")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def track_background_task(task: asyncio.Task, task_name: str, user_id: int) -> None:
    def on_done(completed_task: asyncio.Task) -> None:
        try:
            completed_task.result()
            logger.info("Background task finished: %s, user_id=%s", task_name, user_id)
        except Exception:
            logger.exception("Background task failed: %s, user_id=%s", task_name, user_id)

    task.add_done_callback(on_done)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id != config.ADMIN_ID:
        return

    try:
        await db.create_tables()
    except Exception:
        logger.exception("Failed to initialize database on /start")
        await message.answer("❌ Не удалось подготовить базу данных.")
        return

    await message.answer(
        "👋 **Привет! Это бот-копировщик.**\n\n"
        "Он умеет:\n"
        "1. Копировать посты (текст, фото, альбомы).\n"
        "2. Скрывать автора и менять ссылки (даже скрытые).\n\n"
        "Начни с подключения аккаунта 👇",
        reply_markup=get_main_keyboard(message.from_user.id),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "auth_start")
async def auth_step_1(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    session_path = get_session_path(user_id)

    if os.path.exists(f"{session_path}.session"):
        try:
            os.remove(f"{session_path}.session")
            logger.info("Old session removed for user_id=%s", user_id)
        except OSError:
            logger.exception("Failed to remove old session for user_id=%s", user_id)

    await callback.message.answer("📱 Введите номер телефона (например: +79001234567):")
    await state.set_state(Form.phone)
    await callback.answer()


@dp.message(Form.phone)
async def auth_step_2(message: types.Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "")
    user_id = message.from_user.id

    ensure_sessions_dir()

    status_message = await message.answer("⏳ Соединяюсь с Telegram...")

    client = Client(
        name=get_session_path(user_id),
        api_id=config.API_ID,
        api_hash=config.API_HASH
    )

    try:
        await client.connect()
        sent_code = await client.send_code(phone)

        auth_sessions[user_id] = {
            "client": client,
            "phone": phone,
            "hash": sent_code.phone_code_hash
        }

        logger.info("Authorization code sent for user_id=%s", user_id)

        await status_message.edit_text("✅ Код отправлен! Введите код из Telegram (цифры):")
        await state.set_state(Form.code)

    except Exception:
        logger.exception("Failed to send auth code for user_id=%s", user_id)
        await status_message.edit_text("❌ Не удалось отправить код. Проверьте номер и попробуйте снова.")
        try:
            await client.disconnect()
        except Exception:
            logger.exception("Failed to disconnect client after auth error for user_id=%s", user_id)


@dp.message(Form.code)
async def auth_step_3(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    code = message.text.strip()

    if user_id not in auth_sessions:
        await message.answer("❌ Сессия авторизации не найдена. Начните заново.")
        await state.clear()
        return

    session_data = auth_sessions[user_id]
    client = session_data["client"]

    try:
        await client.sign_in(
            phone_number=session_data["phone"],
            phone_code_hash=session_data["hash"],
            phone_code=code
        )
        await client.disconnect()
        auth_sessions.pop(user_id, None)

        logger.info("User authorized successfully: user_id=%s", user_id)

        await message.answer(
            "🎉 **Аккаунт успешно подключен!**",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="Markdown"
        )
        await state.clear()

    except errors.SessionPasswordNeeded:
        await message.answer("🔐 У вас включен облачный пароль. Введите его:")
        await state.set_state(Form.password)

    except Exception:
        logger.exception("Failed to confirm auth code for user_id=%s", user_id)
        await message.answer("❌ Не удалось подтвердить код. Попробуйте снова.")


@dp.message(Form.password)
async def auth_step_4(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    password = message.text

    if user_id not in auth_sessions:
        await state.clear()
        await message.answer("❌ Сессия авторизации не найдена. Начните заново.")
        return

    client = auth_sessions[user_id]["client"]

    try:
        await client.check_password(password=password)
        await client.disconnect()
        auth_sessions.pop(user_id, None)

        logger.info("2FA password accepted for user_id=%s", user_id)

        await message.answer(
            "🎉 **Пароль принят! Аккаунт подключен.**",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="Markdown"
        )
        await state.clear()

    except Exception:
        logger.exception("Failed to verify 2FA password for user_id=%s", user_id)
        await message.answer("❌ Не удалось подтвердить пароль. Попробуйте снова.")


@dp.callback_query(F.data == "set_source")
async def set_source(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📥 Пришлите **ссылку** или **ID** канала-источника (откуда берем посты):",
        parse_mode="Markdown"
    )
    await state.set_state(Form.source)
    await callback.answer()


@dp.message(Form.source)
async def save_source(message: types.Message, state: FSMContext):
    value = message.text.strip()
    if "t.me/" in value:
        value = value.split("t.me/")[-1].replace("/", "")

    try:
        await db.update_setting(message.from_user.id, "source_chat", value)
        await message.answer(
            f"✅ Источник сохранен: `{value}`",
            reply_markup=get_main_keyboard(message.from_user.id),
            parse_mode="Markdown"
        )
        logger.info("Source saved for user_id=%s", message.from_user.id)
    except Exception:
        logger.exception("Failed to save source for user_id=%s", message.from_user.id)
        await message.answer("❌ Не удалось сохранить источник.")
    finally:
        await state.clear()


@dp.callback_query(F.data == "set_target")
async def set_target(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "Пришлите **ссылку** или **ID** вашего канала (куда постим):",
        parse_mode="Markdown"
    )
    await state.set_state(Form.target)
    await callback.answer()


@dp.message(Form.target)
async def save_target(message: types.Message, state: FSMContext):
    value = message.text.strip()
    if "t.me/" in value:
        value = value.split("t.me/")[-1].replace("/", "")

    try:
        await db.update_setting(message.from_user.id, "target_chat", value)
        await message.answer(
            f"✅ Цель сохранена: `{value}`",
            reply_markup=get_main_keyboard(message.from_user.id),
            parse_mode="Markdown"
        )
        logger.info("Target saved for user_id=%s", message.from_user.id)
    except Exception:
        logger.exception("Failed to save target for user_id=%s", message.from_user.id)
        await message.answer("❌ Не удалось сохранить цель.")
    finally:
        await state.clear()


@dp.callback_query(F.data == "add_replace")
async def add_replacement_step_1(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("🔍 Введите текст (или ссылку), который нужно **НАЙТИ**:")
    await state.set_state(Form.replace_old)
    await callback.answer()


@dp.message(Form.replace_old)
async def add_replacement_step_2(message: types.Message, state: FSMContext):
    await state.update_data(old_txt=message.text)
    await message.answer(
        "Введите текст (или ссылку), на который нужно **ЗАМЕНИТЬ**:\n"
        "(Отправьте `-` чтобы просто удалить текст)"
    )
    await state.set_state(Form.replace_new)


@dp.message(Form.replace_new)
async def add_replacement_step_3(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        old_text = data["old_txt"]
        new_text = "" if message.text.strip() == "-" else message.text

        await db.add_replacement(message.from_user.id, old_text, new_text)
        await message.answer(
            f"✅ Замена добавлена!\n`{old_text}` ➡️ `{new_text}`",
            reply_markup=get_main_keyboard(message.from_user.id),
            parse_mode="Markdown"
        )
        logger.info("Replacement added for user_id=%s", message.from_user.id)
    except Exception:
        logger.exception("Failed to add replacement for user_id=%s", message.from_user.id)
        await message.answer("❌ Не удалось добавить замену.")
    finally:
        await state.clear()


@dp.callback_query(F.data == "list_replaces")
async def list_replacements(callback: types.CallbackQuery):
    try:
        replacements = await db.get_replacements(callback.from_user.id)

        if not replacements:
            await callback.message.answer(
                "Список замен пуст.",
                reply_markup=get_main_keyboard(callback.from_user.id)
            )
        else:
            lines = ["📋 **Ваши замены:**", ""]
            for old_text, new_text in replacements.items():
                lines.append(f" `{old_text}` ➡️ `{new_text}`")

            await callback.message.answer(
                "\n".join(lines),
                reply_markup=get_main_keyboard(callback.from_user.id),
                parse_mode="Markdown"
            )

    except Exception:
        logger.exception("Failed to list replacements for user_id=%s", callback.from_user.id)
        await callback.message.answer("❌ Не удалось получить список замен.")
    finally:
        await callback.answer()


@dp.callback_query(F.data == "clear_replaces")
async def clear_replacements(callback: types.CallbackQuery):
    try:
        await db.clear_replacements(callback.from_user.id)
        await callback.message.answer(
            "🗑 Все замены удалены.",
            reply_markup=get_main_keyboard(callback.from_user.id)
        )
        logger.info("Replacements cleared for user_id=%s", callback.from_user.id)
    except Exception:
        logger.exception("Failed to clear replacements for user_id=%s", callback.from_user.id)
        await callback.message.answer("❌ Не удалось очистить замены.")
    finally:
        await callback.answer()


@dp.callback_query(F.data == "start_worker")
async def start_worker_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if not os.path.exists(f"{get_session_path(user_id)}.session"):
        await callback.message.answer("❌ Сначала подключите аккаунт (кнопка 1)!")
        await callback.answer()
        return

    await callback.message.answer(
        "**Процесс копирования постов запущен!**\n"
        "Бот сейчас очистит чат, скачает посты и начнет копирование.\n"
        "Ждите уведомления о завершении.",
        parse_mode="Markdown"
    )

    task = asyncio.create_task(copy_worker.start_copying_process(user_id, bot, mode="posts"))
    track_background_task(task, "copy_posts", user_id)

    await callback.answer()


async def main():
    try:
        ensure_sessions_dir()
        await db.create_tables()
        logger.info("Bot started.")
        await dp.start_polling(bot)
    except Exception:
        logger.exception("Bot stopped due to critical error.")
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")