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
MAX_MEMORY_CHARS = 3000
MAX_INJECT_CHARS = 1500
COMPRESS_THRESHOLD = 3000


class GroupFileMemory:
    def __init__(self, db: Database, llm_client: LLMClient, settings=None):
        self.db = db
        self.llm_client = llm_client
        self.inject_chars = settings.memory_inject_chars if settings else MAX_INJECT_CHARS
        self.max_chars = settings.memory_max_chars if settings else MAX_MEMORY_CHARS
        self.compress_threshold = settings.memory_max_chars if settings else COMPRESS_THRESHOLD
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
            return raw[:self.inject_chars]

        entries.sort(key=lambda e: e[0], reverse=True)

        result = []
        total = 0
        for score, text in entries:
            line = f"- {text}"
            if total + len(line) + 1 > self.inject_chars:
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
            print(f"[Memory] Group {group_id}: only {len(rows)} msgs (< 10), skip")
            return

        import re
        url_pattern = re.compile(r'https?://\S+')
        messages_text = ""
        for row in reversed(rows):
            tag = "[BOT]" if row[2] else f"[{row[0]}]"
            content = url_pattern.sub("[图片]", row[1]).strip()
            if not content:
                continue
            messages_text += f"{tag}: {content}\n"

        existing = self.load(group_id)

        prompt = f"""你是一个群聊观察者。请根据聊天记录，为这个群生成群记忆档案。

你需要记录以下几类信息：

**群氛围**（0.8-1.0分）：
- 群的整体风格、话题倾向
- 核心人物是谁、谁话多谁潜水

**人物关系**（0.7-0.9分）：
- 谁和谁关系好、谁和谁经常互喷
- 群里的小团体或固定搭配

**群梗话题**（0.5-0.8分）：
- 群里经常聊的话题、反复出现的梗
- 最近的热门话题

**我的行为**（0.8-1.0分）：
- 我在这个群的说话风格（话多还是话少）
- 我和谁互动多、和谁互动少
- 我在这个群的习惯

格式要求（严格遵守）：
- 每行一条记忆，格式：[分数] {tag}: 内容
- 分数保留一位小数，范围 0.1-1.0
- tag 只能是：氛围/关系/梗/行为
- 最多20条，不要重复
- 示例：[0.9] 氛围: 二次元浓度高，经常深夜聊天
- 示例：[0.8] 关系: 泷墨和小夜经常互怼但关系好
- 示例：[0.7] 梗: 经常说"绷不住了"
- 示例：[0.9] 行为: 话多，爱接梗，经常潜水后突然冒泡

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
                if len(new_memory) > self.compress_threshold:
                    new_memory = await self._compress(group_id, new_memory)
                self.save(group_id, new_memory)
                logger.info("Group memory summarized for group %s", group_id)
                print(f"[Memory] Group {group_id} saved")
        except Exception as e:
            logger.error("Failed to summarize group memory: %s", e)

    async def _compress(self, group_id: int, raw: str) -> str:
        entries = self._parse_entries(raw)
        if not entries:
            return raw[:self.max_chars]

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
                if len(compressed) <= self.max_chars:
                    return compressed
        except Exception as e:
            logger.error("Compress failed for group %s: %s", group_id, e)

        entries.sort(key=lambda e: e[0], reverse=True)
        result = []
        total = 0
        for score, text in entries:
            line = f"[{score}] {text}"
            if total + len(line) + 1 > self.max_chars:
                break
            result.append(line)
            total += len(line) + 1
        return "\n".join(result)
