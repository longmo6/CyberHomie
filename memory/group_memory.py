from __future__ import annotations

from memory.database import Database
from utils.logger import setup_logger

logger = setup_logger("group_memory")


class GroupMemory:
    def __init__(self, db: Database):
        self.db = db

    async def save_message(
        self, group_id: int, user_id: int, nickname: str,
        content: str, is_bot: bool = False,
    ):
        await self.db.execute(
            "INSERT INTO chat_history (group_id, user_id, nickname, content, is_bot) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_id, user_id, nickname, content, int(is_bot)),
        )
        await self.db.commit()

    async def get_recent_messages(
        self, group_id: int, limit: int = 20
    ) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT nickname, content, is_bot, timestamp FROM chat_history "
            "WHERE group_id = ? ORDER BY timestamp DESC LIMIT ?",
            (group_id, limit),
        )
        messages = []
        for row in reversed(rows):
            role = "assistant" if row[2] else "user"
            messages.append({
                "role": role,
                "content": row[1],
                "nickname": row[0],
            })
        return messages

    async def save_memory(
        self, group_id: int, memory_type: str,
        content: str, importance: float = 0.5,
    ):
        await self.db.execute(
            "INSERT INTO group_memory (group_id, memory_type, content, importance) "
            "VALUES (?, ?, ?, ?)",
            (group_id, memory_type, content, importance),
        )
        await self.db.commit()
        logger.info("Saved group memory: [%s] %s", memory_type, content[:50])

    async def get_important_memories(
        self, group_id: int, limit: int = 5
    ) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT content FROM group_memory WHERE group_id = ? "
            "ORDER BY importance DESC LIMIT ?",
            (group_id, limit),
        )
        return [row[0] for row in rows]

    async def get_messages_for_summary(
        self, group_id: int, limit: int = 100
    ) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT nickname, content, is_bot, timestamp FROM chat_history "
            "WHERE group_id = ? AND is_bot = 0 ORDER BY timestamp DESC LIMIT ?",
            (group_id, limit),
        )
        return [
            {"nickname": row[0], "content": row[1], "timestamp": row[3]}
            for row in reversed(rows)
        ]
