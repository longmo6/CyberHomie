from __future__ import annotations

import os
import sqlite3
from typing import Optional

import aiosqlite

from utils.logger import setup_logger

logger = setup_logger("database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    qq_id INTEGER PRIMARY KEY,
    nickname TEXT NOT NULL,
    personality_notes TEXT DEFAULT '',
    interests TEXT DEFAULT '',
    emotional_tendency TEXT DEFAULT '',
    quirks TEXT DEFAULT '',
    closeness_score REAL DEFAULT 0.0,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    message_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    content TEXT NOT NULL,
    is_bot INTEGER DEFAULT 0,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_group_time
ON chat_history(group_id, timestamp);

CREATE TABLE IF NOT EXISTS group_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    importance REAL DEFAULT 0.5,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS relationships (
    user_a INTEGER NOT NULL,
    user_b INTEGER NOT NULL,
    relationship_type TEXT DEFAULT 'neutral',
    notes TEXT DEFAULT '',
    PRIMARY KEY (user_a, user_b)
);
"""


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: aiosqlite.Connection | None = None

    async def initialize(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.executescript(SCHEMA)
        # Add nicknames column if not exists
        try:
            await self.db.execute("ALTER TABLE users ADD COLUMN nicknames TEXT DEFAULT ''")
        except Exception:
            pass  # column already exists
        await self.db.commit()
        logger.info("Database initialized: %s", self.db_path)

    async def close(self):
        if self.db:
            await self.db.close()

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        return await self.db.execute(sql, params)

    async def fetchone(self, sql: str, params: tuple = ()):
        cursor = await self.db.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()):
        cursor = await self.db.execute(sql, params)
        return await cursor.fetchall()

    async def commit(self):
        await self.db.commit()
