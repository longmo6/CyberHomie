from __future__ import annotations

import os

from memory.database import Database
from llm.mimo import LLMClient
from utils.logger import setup_logger

logger = setup_logger("group_file_memory")

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "group_memory")


class GroupFileMemory:
    def __init__(self, db: Database, llm_client: LLMClient):
        self.db = db
        self.llm_client = llm_client
        os.makedirs(MEMORY_DIR, exist_ok=True)

    def _get_path(self, group_id: int) -> str:
        return os.path.join(MEMORY_DIR, f"{group_id}.md")

    def load(self, group_id: int) -> str:
        path = self._get_path(group_id)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        return ""

    def save(self, group_id: int, content: str):
        path = self._get_path(group_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.debug("Saved group memory: %s", path)

    async def summarize_and_save(self, group_id: int):
        """Summarize group chat into long-term group memory."""
        rows = await self.db.fetchall(
            "SELECT nickname, content, is_bot, timestamp FROM chat_history "
            "WHERE group_id = ? ORDER BY timestamp DESC LIMIT 200",
            (group_id,),
        )
        if len(rows) < 10:
            return

        messages_text = ""
        for row in reversed(rows):
            tag = "[BOT]" if row[2] else f"[{row[0]}]"
            messages_text += f"{tag}: {row[1]}\n"

        existing = self.load(group_id)

        prompt = f"""你是一个群聊观察者。请根据以下聊天记录，为这个群生成一份群记忆档案。

要求：
- 记录这个群的整体氛围和聊天风格
- 记录群里的核心人物和他们之间的关系
- 记录大家如何互称（谁叫谁什么昵称）
- 记录群里的梗、话题倾向
- 记录谁和bot关系好/差
- 如果有之前的档案，在此基础上更新
- 输出纯文本，用简单条目
- 不超过20条

之前的档案：
{existing if existing else '(无)'}

聊天记录：
{messages_text}

请输出更新后的完整群记忆："""

        try:
            resp = await self.llm_client.client.chat.completions.create(
                model=self.llm_client.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
                temperature=0.7,
            )
            content = resp.choices[0].message.content
            if content:
                self.save(group_id, content.strip())
                logger.info("Group memory summarized for group %s", group_id)
                print(f"[Memory] Group {group_id} saved")
                print(f"[Memory] Group {group_id} memory updated")
        except Exception as e:
            logger.error("Failed to summarize group memory: %s", e)
