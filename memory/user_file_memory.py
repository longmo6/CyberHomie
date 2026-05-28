"""
用户长期记忆系统

记忆格式：每行一条，带重要度分数
  [0.9] 性格：话多，喜欢接梗
  [0.7] 兴趣：动漫
  [0.3] 最近在忙毕设

注入时按重要度排序，只取 top N（受字符数限制）。
超 1500 字自动压缩。
"""
from __future__ import annotations

import os

from memory.database import Database
from llm.mimo import LLMClient
from utils.logger import setup_logger

logger = setup_logger("user_file_memory")

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "memory")
MAX_MEMORY_CHARS = 1500       # 记忆文件最大字符数
MAX_INJECT_CHARS = 800        # 注入 prompt 的最大字符数
COMPRESS_THRESHOLD = 1500     # 超过此长度触发压缩
SUMMARY_MSG_THRESHOLD = 30    # 积累多少新消息后才总结


class UserFileMemory:
    def __init__(self, db: Database, llm_client: LLMClient):
        self.db = db
        self.llm_client = llm_client
        os.makedirs(MEMORY_DIR, exist_ok=True)

    def _get_path(self, qq_id: int) -> str:
        return os.path.join(MEMORY_DIR, f"{qq_id}.md")

    def load(self, qq_id: int) -> str:
        """Load raw memory file."""
        path = self._get_path(qq_id)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        return ""

    def save(self, qq_id: int, content: str):
        path = self._get_path(qq_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def load_for_prompt(self, qq_id: int) -> str:
        """Load memory, sorted by importance, truncated to MAX_INJECT_CHARS."""
        raw = self.load(qq_id)
        if not raw:
            return ""

        entries = self._parse_entries(raw)
        if not entries:
            # 旧格式兼容：直接返回原文截断
            return raw[:MAX_INJECT_CHARS]

        # 按重要度降序排列
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
        """Parse memory entries: [(importance, text), ...]"""
        entries = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            # 格式: [0.9] 内容
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

    async def summarize_and_save(self, qq_id: int, nickname: str):
        """Summarize recent chat into structured long-term memory."""
        rows = await self.db.fetchall(
            "SELECT content, is_bot, timestamp FROM chat_history "
            "WHERE user_id = ? ORDER BY timestamp DESC LIMIT 100",
            (qq_id,),
        )
        if len(rows) < 1:
            return

        messages_text = ""
        for row in reversed(rows):
            tag = "[BOT]" if row[1] else f"[{nickname}]"
            messages_text += f"{tag}: {row[0]}\n"

        existing = self.load(qq_id)

        prompt = f"""你是一个群聊观察者。请根据聊天记录，为 {nickname} 生成长期记忆档案。

你需要记录两类信息：

**个人信息：**
- 性格、兴趣、口头禅、情绪倾向
- 和谁关系好、黑历史等

**你和这个人的互动方式（非常重要）：**
- 你和 ta 的相处模式（开玩笑？认真？撒娇？互怼？）
- ta 对你的态度（友好？冷淡？喜欢逗你？）
- 你们之间的梗或暗号
- 你和 ta 聊天时的语气特点

格式要求：
- 每行一条记忆，格式：[重要度] 内容
- 重要度范围 0.1-1.0（1.0=必须记住，0.5=普通，0.1=可遗忘）
- 性格、互动方式等稳定特征给高分（0.8-1.0）
- 近期事件、临时状态给中低分（0.3-0.6）
- 最多15条
- 不要重复，新信息覆盖旧信息

之前的档案：
{existing if existing else '(无)'}

最近的聊天记录（[BOT]是你自己说的话）：
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
                # 检查是否需要压缩
                if len(new_memory) > COMPRESS_THRESHOLD:
                    new_memory = await self._compress(qq_id, nickname, new_memory)
                self.save(qq_id, new_memory)
                logger.info("Memory summarized for %s (%s)", nickname, qq_id)
                print(f"[Memory] {nickname} ({qq_id}) saved")
        except Exception as e:
            logger.error("Failed to summarize memory for %s: %s", nickname, e)

    async def _compress(self, qq_id: int, nickname: str, raw: str) -> str:
        """Compress memory by keeping only high-importance entries."""
        entries = self._parse_entries(raw)
        if not entries:
            return raw[:MAX_MEMORY_CHARS]

        prompt = f"""以下是 {nickname} 的记忆档案，条目太多需要精简。

要求：
- 保留重要度高的条目（0.7以上必须保留）
- 合并相似条目
- 去除过时或重复的信息
- 最终不超过10条
- 保持同样的格式：[重要度] 内容

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
            logger.error("Compress failed for %s: %s", nickname, e)

        # 兜底：按重要度截断
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

    def list_all(self) -> list[dict]:
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
