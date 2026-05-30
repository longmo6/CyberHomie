from __future__ import annotations

from memory.database import Database
from utils.logger import setup_logger

logger = setup_logger("relationship")


class RelationshipGraph:
    def __init__(self, db: Database):
        self.db = db

    async def get_bot_relationship(self, user_id: int) -> str:
        row = await self.db.fetchone(
            "SELECT closeness_score FROM users WHERE qq_id = ?",
            (user_id,),
        )
        if not row:
            return "不认识"

        score = row[0]
        if score >= 0.8:
            return "很熟的朋友"
        elif score >= 0.5:
            return "认识"
        elif score >= 0.2:
            return "见过几次"
        return "不太熟"

    async def update_closeness(self, user_id: int, delta: float):
        row = await self.db.fetchone(
            "SELECT closeness_score FROM users WHERE qq_id = ?",
            (user_id,),
        )
        if not row:
            return

        new_score = max(0.0, min(1.0, row[0] + delta))
        await self.db.execute(
            "UPDATE users SET closeness_score = ? WHERE qq_id = ?",
            (new_score, user_id),
        )
        await self.db.commit()
        print(f"[Relationship] user {user_id}: {row[0]:.2f} -> {new_score:.2f}")

    async def get_user_relationship(
        self, user_a: int, user_b: int
    ) -> dict | None:
        row = await self.db.fetchone(
            "SELECT relationship_type, notes FROM relationships "
            "WHERE (user_a = ? AND user_b = ?) OR (user_a = ? AND user_b = ?)",
            (user_a, user_b, user_b, user_a),
        )
        if row:
            return {"type": row[0], "notes": row[1]}
        return None

    async def update_relationship(
        self, user_a: int, user_b: int,
        rel_type: str = "neutral", notes: str = "",
    ):
        await self.db.execute(
            "INSERT OR REPLACE INTO relationships (user_a, user_b, relationship_type, notes) "
            "VALUES (?, ?, ?, ?)",
            (user_a, user_b, rel_type, notes),
        )
        await self.db.commit()
