from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import Settings
from llm.mimo import LLMClient
from memory.database import Database
from memory.group_memory import GroupMemory
from memory.user_memory import UserMemory
from utils.logger import setup_logger

logger = setup_logger("scheduler")


class BackgroundScheduler:
    def __init__(
        self,
        db: Database,
        user_memory: UserMemory,
        group_memory: GroupMemory,
        llm_client: LLMClient,
        settings: Settings,
    ):
        self.db = db
        self.user_memory = user_memory
        self.group_memory = group_memory
        self.llm_client = llm_client
        self.group_ids = settings.group_ids
        self.scheduler = AsyncIOScheduler()

    def start(self):
        self.scheduler.add_job(
            self.summarize_recent_chat,
            "interval",
            hours=2,
            id="summarize_chat",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.update_user_profiles,
            "interval",
            hours=6,
            id="update_profiles",
            replace_existing=True,
        )
        self.scheduler.start()
        logger.info("Background scheduler started")

    def shutdown(self):
        self.scheduler.shutdown()
        logger.info("Background scheduler stopped")

    async def summarize_recent_chat(self):
        try:
            print("\n[Scheduler] Summarizing recent chat...")
            for gid in self.group_ids:
                messages = await self.group_memory.get_messages_for_summary(gid, limit=100)
                if len(messages) < 5:
                    print(f"[Scheduler] Group {gid}: not enough messages, skip")
                    continue
                summary = await self.llm_client.summarize_chat(messages)
                if summary:
                    await self.group_memory.save_memory(gid, "summary", summary, importance=0.7)
                    print(f"[Scheduler] Group {gid} summary: {summary[:100]}")
        except Exception as e:
            logger.error("Summarize job failed: %s", e)

    async def update_user_profiles(self):
        try:
            print("\n[Scheduler] Updating user profiles...")
            rows = await self.db.fetchall(
                "SELECT qq_id, nickname FROM users "
                "WHERE message_count >= 3 "
                "AND last_seen > datetime('now', '-1 day') "
                "ORDER BY last_seen DESC LIMIT 20"
            )
            for row in rows:
                qq_id, nickname = row[0], row[1]
                messages = await self.db.fetchall(
                    "SELECT content FROM chat_history "
                    "WHERE user_id = ? AND is_bot = 0 "
                    "ORDER BY timestamp DESC LIMIT 50",
                    (qq_id,),
                )
                msg_list = [{"content": m[0]} for m in messages]
                if len(msg_list) < 5:
                    continue

                analysis = await self.llm_client.analyze_user(nickname, msg_list)
                if analysis:
                    await self.user_memory.update_user(
                        qq_id,
                        personality_notes=analysis.get("personality_notes", ""),
                        interests=analysis.get("interests", ""),
                        emotional_tendency=analysis.get("emotional_tendency", ""),
                        quirks=analysis.get("quirks", ""),
                    )
                    logger.info("Updated profile for %s", nickname)
                    print(f"[Scheduler] Updated profile: {nickname}")
        except Exception as e:
            logger.error("Update profiles job failed: %s", e)
