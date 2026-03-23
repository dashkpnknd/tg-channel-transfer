import asyncio
import logging
import random
import re
from typing import List, Optional, Union

from pyrogram import Client, enums, errors
from pyrogram.raw.functions.messages import ForwardMessages
from pyrogram.types import Message

import database_handler as db
import local_config as config

logger = logging.getLogger(__name__)


def prepare_chat_id(chat_id: Optional[Union[str, int]]) -> Optional[Union[str, int]]:
    if not chat_id:
        return None

    value = str(chat_id).strip()
    if re.match(r"^-?\d+$", value):
        return int(value)
    return value


def replace_text_in_html(html_text: str, replacements: dict) -> str:
    if not html_text:
        return ""

    result = html_text

    for source_text, target_text in replacements.items():
        old_value = str(source_text).strip()
        new_value = str(target_text).strip()

        if not old_value:
            continue

        escaped_old = re.escape(old_value)

        tag_pattern = r'(?i)<([a-z0-9]+)[^>]*>\s*' + escaped_old + r'\s*</\1>'
        result = re.sub(tag_pattern, new_value, result)

        link_pattern = r'(?i)<a[^>]*>[^<]*' + escaped_old + r'[^<]*</a>'
        result = re.sub(link_pattern, new_value, result)

        text_pattern = r'(?i)' + escaped_old
        result = re.sub(text_pattern, new_value, result)

    return result


async def get_chat_safe(app: Client, chat_id: Union[str, int]):
    try:
        return await app.get_chat(chat_id)
    except Exception:
        logger.warning("get_chat failed for chat_id=%s, fallback to dialogs", chat_id)
        async for dialog in app.get_dialogs():
            if str(dialog.chat.id) == str(chat_id):
                return dialog.chat
        raise


async def load_full_history(
    app: Client,
    chat_id: Union[str, int],
    batch_size: int = 1000
) -> List[Message]:
    messages: List[Message] = []
    offset_id = 0

    while True:
        batch: List[Message] = []

        async for message in app.get_chat_history(
            chat_id,
            limit=batch_size,
            offset_id=offset_id
        ):
            batch.append(message)

        if not batch:
            break

        messages.extend(batch)
        offset_id = batch[-1].id
        await asyncio.sleep(0.15)

    messages.reverse()
    return messages


async def forward_without_author(
    app: Client,
    source_chat: Union[str, int],
    target_chat: Union[str, int],
    message_ids: List[int]
) -> List[Message]:
    try:
        from_peer = await app.resolve_peer(source_chat)
        to_peer = await app.resolve_peer(target_chat)
        random_ids = [app.rnd_id() for _ in message_ids]

        result = await app.invoke(
            ForwardMessages(
                from_peer=from_peer,
                id=message_ids,
                random_id=random_ids,
                to_peer=to_peer,
                drop_author=True
            )
        )

        await asyncio.sleep(5)

        new_ids: List[int] = []
        if hasattr(result, "updates"):
            for update in result.updates:
                message = getattr(update, "message", None)
                if message and getattr(message, "id", None):
                    new_ids.append(message.id)

        if not new_ids:
            return []

        new_ids.sort()
        messages = await app.get_messages(target_chat, new_ids)

        if isinstance(messages, list):
            return [message for message in messages if message is not None]

        return [messages] if messages is not None else []

    except Exception:
        logger.exception(
            "Failed to forward messages without author: source=%s, target=%s, ids=%s",
            source_chat,
            target_chat,
            message_ids
        )
        return []


async def clear_target_chat(
    app: Client,
    target_chat: Union[str, int],
    bot,
    user_id: int
) -> None:
    await bot.send_message(user_id, "Очищаю чат...")

    try:
        message_ids = [message.id async for message in app.get_chat_history(target_chat, limit=200)]
        if not message_ids:
            return

        for index in range(0, len(message_ids), 100):
            chunk = message_ids[index:index + 100]
            await app.delete_messages(target_chat, chunk)
            await asyncio.sleep(1)

        logger.info("Target chat cleaned: target_chat=%s, deleted=%s", target_chat, len(message_ids))
    except Exception:
        logger.exception("Failed to clear target chat: target_chat=%s", target_chat)
        await bot.send_message(user_id, "⚠️ Не удалось полностью очистить целевой чат.")


async def update_forwarded_message_text(sent_message: Message, replacements: dict) -> None:
    if not replacements:
        return

    old_html = ""
    content_type = None

    if sent_message.text:
        old_html = sent_message.text.html
        content_type = "text"
    elif sent_message.caption:
        old_html = sent_message.caption.html
        content_type = "caption"

    if not content_type:
        return

    new_html = replace_text_in_html(old_html, replacements)

    if new_html == old_html:
        return

    if content_type == "text":
        await sent_message.edit_text(new_html, parse_mode=enums.ParseMode.HTML)
    else:
        await sent_message.edit_caption(new_html, parse_mode=enums.ParseMode.HTML)


async def start_copying_process(user_id, bot, mode="posts"):
    try:
        settings = await db.get_settings(user_id)
    except Exception:
        logger.exception("Failed to load settings for user_id=%s", user_id)
        await bot.send_message(user_id, "❌ Не удалось загрузить настройки.")
        return

    if not settings:
        await bot.send_message(user_id, "❌ Нет настроек.")
        return

    try:
        replacements = await db.get_replacements(user_id) or {}
    except Exception:
        logger.exception("Failed to load replacements for user_id=%s", user_id)
        await bot.send_message(user_id, "❌ Не удалось загрузить список замен.")
        return

    source_chat = prepare_chat_id(settings.get("source_chat"))
    target_chat = prepare_chat_id(settings.get("target_chat"))

    if not source_chat or not target_chat:
        await bot.send_message(user_id, "❌ Не задан источник или цель.")
        return

    app = Client(
        name=f"sessions/user_{user_id}",
        api_id=config.API_ID,
        api_hash=config.API_HASH
    )

    try:
        async with app:
            await get_chat_safe(app, source_chat)
            await get_chat_safe(app, target_chat)

            await clear_target_chat(app, target_chat, bot, user_id)

            await bot.send_message(user_id, "Считываю историю")
            raw_messages = await load_full_history(app, source_chat, batch_size=1000)

            tasks = []
            current_album: List[Message] = []
            current_group_id = None

            for message in raw_messages:
                if getattr(message, "service", False) or getattr(message, "empty", False):
                    continue

                if message.media_group_id:
                    if current_group_id == message.media_group_id:
                        current_album.append(message)
                    else:
                        if current_album:
                            tasks.append({"type": "album", "parts": current_album})
                        current_group_id = message.media_group_id
                        current_album = [message]
                else:
                    if current_album:
                        tasks.append({"type": "album", "parts": current_album})
                        current_album = []
                        current_group_id = None
                    tasks.append({"type": "single", "msg": message})

            if current_album:
                tasks.append({"type": "album", "parts": current_album})

            total = len(tasks)
            await bot.send_message(user_id, f"✅ Найдено постов: {total}. Начинаем копирование")

            for index, task in enumerate(tasks, start=1):
                try:
                    await asyncio.sleep(random.randint(1, 3))

                    if task["type"] == "album":
                        message_ids = [part.id for part in task["parts"]]
                        sent_messages = await forward_without_author(
                            app,
                            source_chat,
                            target_chat,
                            message_ids
                        )

                        if replacements and sent_messages:
                            for sent_message in sent_messages:
                                if sent_message.caption:
                                    old_html = sent_message.caption.html
                                    new_html = replace_text_in_html(old_html, replacements)

                                    if new_html != old_html:
                                        try:
                                            await sent_message.edit_caption(
                                                new_html,
                                                parse_mode=enums.ParseMode.HTML
                                            )
                                        except Exception:
                                            logger.exception(
                                                "Failed to edit album caption: user_id=%s, message_id=%s",
                                                user_id,
                                                sent_message.id
                                            )

                        logger.info("Album copied: %s/%s", index, total)

                    else:
                        source_message = task["msg"]
                        sent_messages = await forward_without_author(
                            app,
                            source_chat,
                            target_chat,
                            [source_message.id]
                        )

                        if sent_messages and replacements:
                            try:
                                await update_forwarded_message_text(sent_messages[0], replacements)
                            except Exception:
                                logger.exception(
                                    "Failed to edit forwarded message: user_id=%s, source_message_id=%s",
                                    user_id,
                                    source_message.id
                                )

                        logger.info("Message copied: %s/%s", index, total)

                except errors.FloodWait as flood_wait:
                    wait_seconds = flood_wait.value + 2
                    logger.warning(
                        "FloodWait detected: user_id=%s, step=%s/%s, wait=%s",
                        user_id,
                        index,
                        total,
                        wait_seconds
                    )
                    await asyncio.sleep(wait_seconds)

                except Exception:
                    logger.exception(
                        "Failed at step %s/%s for user_id=%s",
                        index,
                        total,
                        user_id
                    )

            await bot.send_message(user_id, f"🏁 Все! Перенесено {total} постов.")

    except Exception:
        logger.exception("Critical error in copying process for user_id=%s", user_id)
        await bot.send_message(user_id, "❌ Произошла ошибка во время копирования.")