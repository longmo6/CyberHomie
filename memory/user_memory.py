from __future__ import annotations

from memory.database import Database
from utils.logger import setup_logger

logger = setup_logger("user_memory")


class UserMemory:
    def __init__(self, db: Database):
        self.db = db

    async def get_or_create_user(self, qq_id: int, nickname: str) -> dict:
        row = await self.db.fetchone(
            "SELECT * FROM users WHERE qq_id = ?", (qq_id,)
        )
        if row:
            return dict(row)

        await self.db.execute(
            "INSERT OR IGNORE INTO users (qq_id, nickname) VALUES (?, ?)",
            (qq_id, nickname),
        )
        await self.db.commit()
        row = await self.db.fetchone(
            "SELECT * FROM users WHERE qq_id = ?", (qq_id,)
        )
        logger.info("New user created: %s (%s)", nickname, qq_id)
        return dict(row)

    async def update_user(self, qq_id: int, **kwargs):
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [qq_id]
        await self.db.execute(
            f"UPDATE users SET {sets} WHERE qq_id = ?", tuple(values)
        )
        await self.db.commit()

    async def increment_message_count(self, qq_id: int):
        await self.db.execute(
            "UPDATE users SET message_count = message_count + 1, "
            "last_seen = CURRENT_TIMESTAMP WHERE qq_id = ?",
            (qq_id,),
        )
        await self.db.commit()

    async def get_user_summary(self, qq_id: int) -> str:
        row = await self.db.fetchone(
            "SELECT nickname, personality_notes, interests, "
            "emotional_tendency, quirks, closeness_score, message_count, nicknames "
            "FROM users WHERE qq_id = ?",
            (qq_id,),
        )
        if not row:
            return ""

        parts = []
        if row[7]:
            parts.append(f"昵称: {row[7]}")
        if row[1]:
            parts.append(f"性格: {row[1]}")
        if row[2]:
            parts.append(f"兴趣: {row[2]}")
        if row[3]:
            parts.append(f"情绪: {row[3]}")
        if row[4]:
            parts.append(f"口头禅: {row[4]}")
        if row[5] > 0.7:
            parts.append("关系: 很熟")
        elif row[5] > 0.3:
            parts.append("关系: 普通")
        if row[6] > 50:
            parts.append("活跃度: 高")
        elif row[6] > 10:
            parts.append("活跃度: 中")

        return f"{row[0]}: " + ", ".join(parts) if parts else ""
