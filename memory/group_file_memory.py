"""
群长期记忆系统

与用户记忆相同的结构化格式：
  [0.9] 群氛围：二次元为主，经常深夜聊天
  [0.7] 核心人物：泷墨（话多）、xxx（潜水）

注入时按重要度排序截断，超长自动压缩。
"""
from __future__ import annotations

import os

from memory.database import Database
from llm.mimo import LLMClient
from utils.logger import setup_logger

logger = setup_logger("group_file_memory")

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "group_memory")
MAX_MEMORY_CHARS = 1500
MAX_INJECT_CHARS = 800
COMPRESS_THRESHOLD = 1500


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

    def load_for_prompt(self, group_id: int) -> str:
        """Load memory, sorted by importance, truncated."""
        raw = self.load(group_id)
        if not raw:
            return ""

        entries = self._parse_entries(raw)
        if not entries:
            return raw[:MAX_INJECT_CHARS]

        entries.sort(key=lambda e: e[0], reverse=True)

        result = []
        total = 0
        for score, text in entries:
            line = f"- {text}"
            if total + len(line) + 1 > MAX_INJECT_CHARS:
                break
            result.append(line)
            total += len(line) + 1

        return "\n".join(result)

    def _parse_entries(self, raw: str) -> list[tuple[float, str]]:
        entries = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("[") and "]" in line:
                try:
                    bracket_end = line.index("]")
                    score = float(line[1:bracket_end])
                    text = line[bracket_end + 1:].strip()
                    if text:
                        entries.append((score, text))
                except ValueError:
                    entries.append((0.5, line))
            else:
                entries.append((0.5, line))
        return entries

    async def summarize_and_save(self, group_id: int):
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

        prompt = f"""你是一个群聊观察者。请根据聊天记录，为这个群生成群记忆档案。

你需要记录两类信息：

**群信息：**
- 群氛围、核心人物、群梗、话题倾向
- 谁和谁关系好、谁和谁经常互喷

**你在这个群的行为模式（非常重要）：**
- 你在这个群的说话风格（话多还是话少、爱用什么语气）
- 你和谁互动多、和谁互动少
- 你在这个群的习惯（比如经常接梗、经常潜水、经常开某种玩笑）
- 这个群和其他群的不同之处

格式要求：
- 每行一条记忆，格式：[重要度] 内容
- 重要度范围 0.1-1.0（1.0=必须记住，0.5=普通，0.1=可遗忘）
- 群氛围、核心人物、你的行为模式给高分（0.8-1.0）
- 具体事件给中分（0.5-0.7）
- 临时话题给低分（0.1-0.4）
- 最多20条
- 不要重复

之前的档案：
{existing if existing else '(无)'}

聊天记录（[BOT]是你自己说的话）：
{messages_text}

请输出完整的更新后档案（严格按格式）："""

        try:
            resp = await self.llm_client.client.chat.completions.create(
                model=self.llm_client.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
                temperature=0.7,
            )
            content = resp.choices[0].message.content
            if content:
                new_memory = content.strip()
                if len(new_memory) > COMPRESS_THRESHOLD:
                    new_memory = await self._compress(group_id, new_memory)
                self.save(group_id, new_memory)
                logger.info("Group memory summarized for group %s", group_id)
                print(f"[Memory] Group {group_id} saved")
        except Exception as e:
            logger.error("Failed to summarize group memory: %s", e)

    async def _compress(self, group_id: int, raw: str) -> str:
        entries = self._parse_entries(raw)
        if not entries:
            return raw[:MAX_MEMORY_CHARS]

        prompt = f"""以下是群记忆档案，条目太多需要精简。

要求：
- 保留重要度高的条目（0.7以上必须保留）
- 合并相似条目，去除过时信息
- 最终不超过10条
- 保持格式：[重要度] 内容

当前档案：
{raw}

请输出精简后的档案："""

        try:
            resp = await self.llm_client.client.chat.completions.create(
                model=self.llm_client.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.5,
            )
            content = resp.choices[0].message.content
            if content:
                compressed = content.strip()
                if len(compressed) <= MAX_MEMORY_CHARS:
                    return compressed
        except Exception as e:
            logger.error("Compress failed for group %s: %s", group_id, e)

        entries.sort(key=lambda e: e[0], reverse=True)
        result = []
        total = 0
        for score, text in entries:
            line = f"[{score}] {text}"
            if total + len(line) + 1 > MAX_MEMORY_CHARS:
                break
            result.append(line)
            total += len(line) + 1
        return "\n".join(result)
