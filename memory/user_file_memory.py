from __future__ import annotations

import os
from typing import Optional

from memory.database import Database
from llm.mimo import LLMClient
from utils.logger import setup_logger

logger = setup_logger("user_file_memory")

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "memory")


class UserFileMemory:
    def __init__(self, db: Database, llm_client: LLMClient):
        self.db = db
        self.llm_client = llm_client
        os.makedirs(MEMORY_DIR, exist_ok=True)

    def _get_path(self, qq_id: int) -> str:
        return os.path.join(MEMORY_DIR, f"{qq_id}.md")

    def load(self, qq_id: int) -> str:
        """Load user memory from file."""
        path = self._get_path(qq_id)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        return ""

    def save(self, qq_id: int, content: str):
        """Save user memory to file."""
        path = self._get_path(qq_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.debug("Saved memory file: %s", path)

    async def summarize_and_save(self, qq_id: int, nickname: str):
        """Summarize recent chat into long-term memory file."""
        # Get recent messages from this user
        rows = await self.db.fetchall(
            "SELECT content, is_bot, timestamp FROM chat_history "
            "WHERE user_id = ? ORDER BY timestamp DESC LIMIT 100",
            (qq_id,),
        )
        if len(rows) < 1:
            return

        # Build context for summarization
        messages_text = ""
        for row in reversed(rows):
            tag = "[BOT]" if row[1] else f"[{nickname}]"
            messages_text += f"{tag}: {row[0]}\n"

        # Load existing memory
        existing = self.load(qq_id)

        prompt = f"""你是一个群聊观察者。请根据以下聊天记录，为 {nickname} 生成一份长期记忆档案。

要求：
- 用简短的条目记录这个人的特点
- 包括：性格、兴趣、口头禅、情绪倾向、和谁关系好、黑历史等
- 记录大家怎么称呼这个人（昵称、别名等）
- 如果有之前的档案，在此基础上更新
- 输出纯文本，不要markdown格式，用简单的条目列表
- 不超过15条

之前的档案：
{existing if existing else '(无)'}

最近的聊天记录：
{messages_text}

请输出更新后的完整档案："""

        try:
            resp = await self.llm_client.client.chat.completions.create(
                model=self.llm_client.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
                temperature=0.7,
            )
            content = resp.choices[0].message.content
            if content:
                self.save(qq_id, content.strip())
                logger.info("Memory summarized for %s (%s)", nickname, qq_id)
                print(f"[Memory] {nickname} ({qq_id}) saved")
        except Exception as e:
            logger.error("Failed to summarize memory for %s: %s", nickname, e)

    def list_all(self) -> list[dict]:
        """List all user memory files."""
        result = []
        if not os.path.exists(MEMORY_DIR):
            return result
        for f in os.listdir(MEMORY_DIR):
            if f.endswith(".md"):
                qq_id = f[:-3]
                path = os.path.join(MEMORY_DIR, f)
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read().strip()
                result.append({"qq_id": qq_id, "content": content})
        return result
