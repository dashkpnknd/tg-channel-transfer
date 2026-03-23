import logging
from typing import Any, Dict, Optional

import aiosqlite

DB_NAME = "bot_memory.db"

logger = logging.getLogger(__name__)

ALLOWED_SETTINGS_COLUMNS = {
    "source_chat",
    "target_chat",
    "limit_posts",
    "last_msg_id",
}


async def create_tables() -> None:
    try:
        async with aiosqlite.connect(DB_NAME) as connection:
            await connection.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    user_id INTEGER PRIMARY KEY,
                    source_chat TEXT,
                    target_chat TEXT,
                    limit_posts INTEGER DEFAULT 0,
                    last_msg_id INTEGER DEFAULT 0
                )
            """)

            await connection.execute("""
                CREATE TABLE IF NOT EXISTS replacements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    old_text TEXT,
                    new_text TEXT
                )
            """)

            await connection.commit()
            logger.info("Database tables are ready.")
    except aiosqlite.Error:
        logger.exception("Failed to create database tables.")
        raise


async def get_settings(user_id: int) -> Optional[Dict[str, Any]]:
    try:
        async with aiosqlite.connect(DB_NAME) as connection:
            connection.row_factory = aiosqlite.Row

            async with connection.execute(
                "SELECT * FROM settings WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    except aiosqlite.Error:
        logger.exception("Failed to get settings for user_id=%s", user_id)
        raise


async def update_setting(user_id: int, column: str, value: Any) -> None:
    if column not in ALLOWED_SETTINGS_COLUMNS:
        raise ValueError(f"Unsupported settings column: {column}")

    try:
        async with aiosqlite.connect(DB_NAME) as connection:
            async with connection.execute(
                "SELECT 1 FROM settings WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                exists = await cursor.fetchone()

            if not exists:
                await connection.execute(
                    "INSERT INTO settings (user_id) VALUES (?)",
                    (user_id,)
                )

            query = f"UPDATE settings SET {column} = ? WHERE user_id = ?"
            await connection.execute(query, (value, user_id))
            await connection.commit()

            logger.info(
                "Updated setting: user_id=%s, column=%s, value=%r",
                user_id,
                column,
                value
            )

    except aiosqlite.Error:
        logger.exception(
            "Failed to update setting for user_id=%s, column=%s",
            user_id,
            column
        )
        raise


async def add_replacement(user_id: int, old_text: str, new_text: str) -> None:
    try:
        async with aiosqlite.connect(DB_NAME) as connection:
            await connection.execute(
                "INSERT INTO replacements (user_id, old_text, new_text) VALUES (?, ?, ?)",
                (user_id, old_text, new_text)
            )
            await connection.commit()

            logger.info("Added replacement for user_id=%s", user_id)

    except aiosqlite.Error:
        logger.exception("Failed to add replacement for user_id=%s", user_id)
        raise


async def get_replacements(user_id: int) -> Dict[str, str]:
    try:
        async with aiosqlite.connect(DB_NAME) as connection:
            connection.row_factory = aiosqlite.Row

            async with connection.execute(
                "SELECT old_text, new_text FROM replacements WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return {row["old_text"]: row["new_text"] for row in rows}

    except aiosqlite.Error:
        logger.exception("Failed to get replacements for user_id=%s", user_id)
        raise


async def clear_replacements(user_id: int) -> None:
    try:
        async with aiosqlite.connect(DB_NAME) as connection:
            await connection.execute(
                "DELETE FROM replacements WHERE user_id = ?",
                (user_id,)
            )
            await connection.commit()

            logger.info("Cleared replacements for user_id=%s", user_id)

    except aiosqlite.Error:
        logger.exception("Failed to clear replacements for user_id=%s", user_id)
        raise