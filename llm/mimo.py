from __future__ import annotations

import json
import re

from httpx import Timeout
from openai import AsyncOpenAI

from config import Settings
from utils.logger import setup_logger

logger = setup_logger("llm")


class ConversationSession:
    """Session-scoped rolling conversation window."""

    def __init__(self, max_messages: int = 200):
        self.messages: list[dict] = []
        self.max_messages = max_messages

    def add_message(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]

    def get_messages(self, limit: int = None) -> list[dict]:
        if limit:
            return self.messages[-limit:]
        return self.messages

    def update_last_user_message(self, new_content: str):
        """更新最后一条用户消息的内容（用于补充图片描述）"""
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i]["role"] == "user":
                self.messages[i]["content"] = new_content
                break

    def clear(self):
        self.messages.clear()

    def __len__(self):
        return len(self.messages)


class LLMClient:
    def __init__(self, settings: Settings):
        self.client = AsyncOpenAI(
            api_key=settings.mimo_api_key,
            base_url=settings.mimo_base_url,
            timeout=Timeout(60.0, connect=10.0),
        )
        self.model = settings.mimo_model
        self.vision_model = settings.mimo_vision_model
        self.settings = settings

    @staticmethod
    def _build_content(text: str, images: list[str] = None) -> str | list:
        """Build multimodal content if images are present."""
        if not images:
            return text
        content = []
        for url in images:
            content.append({"type": "image_url", "image_url": {"url": url}})
        content.append({"type": "text", "text": text})
        return content

    async def generate_reply(
        self,
        system_prompt: str,
        chat_history: list[dict],
        user_message: str,
    ) -> str:
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(chat_history[-self.settings.ctx_private:])
        messages.append({"role": "user", "content": user_message})

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.85,
                top_p=0.9,
            )
            choice = resp.choices[0]
            content = choice.message.content
            usage = getattr(resp, "usage", None)
            if usage:
                print(f"[LLM] generate_reply ({self.model}): prompt={usage.prompt_tokens}, completion={usage.completion_tokens}")
            if not content:
                reason = getattr(choice, "finish_reason", "unknown")
                print(f"[LLM] generate_reply: content=None, finish_reason={reason}")
                return ""
            return content.strip()
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            print(f"[LLM] generate_reply error: {e}")
            return ""

    async def summarize_chat(self, messages: list[dict]) -> str:
        url_pattern = re.compile(r'https?://\S+')
        prompt = (
            "你是一个群聊观察者。请用2-3句话总结以下聊天记录的要点，"
            "包括谁说了什么重要的事、有什么梗或争论。用中文，随意一点。\n\n"
        )
        for msg in messages:
            sender = msg.get("nickname", "未知")
            content = msg.get("content", "")
            content = url_pattern.sub("[图片]", content).strip()
            if not content:
                continue
            prompt += f"{sender}: {content}\n"

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
            )
            content = resp.choices[0].message.content
            usage = getattr(resp, "usage", None)
            if usage:
                print(f"[LLM] summarize_chat: prompt={usage.prompt_tokens}, completion={usage.completion_tokens}")
            return content.strip() if content else ""
        except Exception as e:
            logger.error("Summarize failed: %s", e)
            print(f"[LLM] summarize_chat error: {e}")
            return ""

    async def analyze_user(self, nickname: str, messages: list[dict]) -> dict:
        url_pattern = re.compile(r'https?://\S+')
        prompt = (
            f"你是一个群聊观察者。根据以下{nickname}的聊天记录，分析这个人的特点。\n"
            "返回JSON格式：\n"
            '{"personality_notes": "性格特点", "interests": "兴趣话题", '
            '"emotional_tendency": "情绪倾向", "quirks": "怪癖或口头禅"}\n\n'
            f"聊天记录：\n"
        )
        for msg in messages:
            content = msg.get('content', '')
            content = url_pattern.sub("[图片]", content).strip()
            if not content:
                continue
            prompt += f"{content}\n"

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            usage = getattr(resp, "usage", None)
            if usage:
                print(f"[LLM] analyze_user: prompt={usage.prompt_tokens}, completion={usage.completion_tokens}")
            return json.loads(content) if content else {}
        except Exception as e:
            logger.error("Analyze user failed: %s", e)
            print(f"[LLM] analyze_user error: {e}")
            return {}

    async def describe_images(self, images: list[str]) -> str:
        """用 vision 模型描述图片内容，返回简短描述"""
        if not images:
            return ""
        print(f"[LLM] describe_images ({self.vision_model}): 处理 {len(images)} 张图片...")
        content = []
        for url in images:
            content.append({"type": "image_url", "image_url": {"url": url}})
        content.append({"type": "text", "text": "简短描述这张图片的内容，一句话即可。"})
        try:
            resp = await self.client.chat.completions.create(
                model=self.vision_model,
                messages=[{"role": "user", "content": content}],
                max_completion_tokens=200,
                temperature=0.3,
            )
            desc = resp.choices[0].message.content
            result = desc.strip() if desc else "[图片]"
            print(f"[LLM] describe_images ({self.vision_model}): {result}")
            return result
        except Exception as e:
            logger.error("describe_images failed: %s", e)
            print(f"[LLM] describe_images error: {e}")
            return "[图片]"

    async def decide_replies(
        self,
        system_prompt: str,
        session_messages: list[dict],
        buffered: list[dict],
    ) -> list[dict]:
        """
        Given buffered messages, decide which to reply to.
        Returns list of {"message_id": int, "text": str, "quote": bool}
        """
        # 构建消息列表（图片描述已在消息文本中）
        msg_list = ""
        for m in buffered:
            at_tag = " [@bot]" if m.get("is_at_bot") else ""
            msg_list += f'[id={m["message_id"]}] {m["nickname"]}{at_tag}: {m["text"]}\n'

        prompt = (
            "以下是你所在群聊最近的消息。你是群里的老群友，不是助手。\n"
            "请决定你要回复哪些消息。可以回复多条，也可以一条都不回。\n\n"
            "规则：\n"
            "- 优先回复最新的消息，不要回复过时的话题\n"
            "- 被@你的消息必须回复\n"
            "- 有人对你说话、提到你、接你的话，都可以回\n"
            "- 你觉得有意思的、想参与的话题，可以回\n"
            "- 合理聊天范围内都可以回，不用刻意沉默\n"
            "- 只有明显和你无关的（别人@别人、别人之间的私聊）才不回\n"
            "- 被质疑是AI时不要死不承认，可以无语、反问、岔开话题\n"
            "- 如果有图片可以回应\n"
            "- 如果你对这个人有记忆，可以自然融入语气，但不要说\"我记得\"\n\n"
            "表情包/贴纸规则（非常重要）：\n"
            "- 纯表情包消息（只有[表情包]没有文字）一律不回\n"
            "- 表情包不需要你解读、回应、评价\n"
            "- 如果对方发了表情包但同时也说了话，只回复文字部分\n"
            "- 绝不要对表情包内容发表看法\n\n"
            "引用回复规则（非常重要）：\n"
            "- 绝大多数回复不要引用（quote=false），直接说就行\n"
            "- 只有被@的时候才引用回复（quote=true）\n"
            "- 其他情况一律quote=false\n\n"
            "消息列表：\n"
            f"{msg_list}\n\n"
            '以JSON格式回复：\n'
            '{"replies": [{"message_id": 消息id, "text": "你的回复", "quote": true/false}]}\n'
            '如果不想回复任何消息：{"replies": []}\n'
            '只输出JSON，不要其他内容。'
        )

        # 始终用 pro 模型做决策（图片已转为文字描述）
        messages_for_llm = [{"role": "system", "content": system_prompt}]
        messages_for_llm.extend(session_messages[-self.settings.ctx_group:])
        messages_for_llm.append({"role": "user", "content": prompt})

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages_for_llm,
                temperature=0.85,
                top_p=0.9,
                response_format={"type": "json_object"},
            )
            choice = resp.choices[0]
            content = choice.message.content
            usage = getattr(resp, "usage", None)
            if usage:
                print(f"[LLM] decide_replies ({self.model}): prompt={usage.prompt_tokens}, completion={usage.completion_tokens}")
            if not content:
                reason = getattr(choice, "finish_reason", "unknown")
                print(f"[LLM] decide_replies: content=None, finish_reason={reason}")
                return []
            data = json.loads(content)
            replies = data.get("replies", [])
            print(f"[LLM] decide_replies: {len(replies)} replies")
            return replies
        except Exception as e:
            logger.error("decide_replies failed: %s", e)
            print(f"[LLM] decide_replies error: {e}")
            return []

    async def generate_topic(
        self, system_prompt: str, session_messages: list[dict]
    ) -> str:
        """Generate a proactive conversation topic when chat has been quiet."""
        prompt = (
            "群聊已经安静了一会儿。你想找个话题聊聊天。\n"
            "根据之前的聊天内容和你对群的了解，随便说点什么开启话题。\n"
            "要求：\n"
            "- 像普通人突然想到什么一样自然地说出来\n"
            '- 不要太正式，不要"大家好"这种\n'
            "- 可以分享个有趣的事、问个问题、吐槽点什么\n"
            "- 一句话就行，不要长篇大论\n\n"
            "你现在想说什么？直接说，不要任何前缀。"
        )

        messages_for_llm = [{"role": "system", "content": system_prompt}]
        messages_for_llm.extend(session_messages[-self.settings.ctx_join:])
        messages_for_llm.append({"role": "user", "content": prompt})

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages_for_llm,
                temperature=0.9,
            )
            choice = resp.choices[0]
            content = choice.message.content
            usage = getattr(resp, "usage", None)
            if usage:
                print(f"[LLM] generate_topic: prompt_tokens={usage.prompt_tokens}, completion_tokens={usage.completion_tokens}, total_tokens={usage.total_tokens}")
            if not content:
                reason = getattr(choice, "finish_reason", "unknown")
                refusal = getattr(choice.message, "refusal", None)
                print(f"[LLM] generate_topic: content=None, finish_reason={reason}, refusal={refusal}")
                return ""
            return content.strip()
        except Exception as e:
            logger.error("generate_topic failed: %s", e)
            print(f"[LLM] generate_topic error: {e}")
            return ""

    async def generate_join_reply(
        self, system_prompt: str, session_messages: list[dict]
    ) -> str:
        """Generate a reply based on recent chat history."""
        prompt = (
            "看看最近的聊天记录，你想说点什么。\n"
            "可以接话、可以吐槽、可以顺着话题聊、也可以从聊天内容里引出新话题。\n"
            "要求：\n"
            "- 像群友随口说话一样自然\n"
            "- 基于聊天内容来说，不要凭空开一个完全无关的话题\n"
            "- 一句话就行，不要太长\n"
            "- 不要重复别人说过的话\n"
            "- 不要太正式，口语化\n\n"
            "你现在想说什么？直接说，不要任何前缀。"
        )

        messages_for_llm = [{"role": "system", "content": system_prompt}]
        messages_for_llm.extend(session_messages[-self.settings.ctx_join:])
        messages_for_llm.append({"role": "user", "content": prompt})

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages_for_llm,
                temperature=0.9,
            )
            choice = resp.choices[0]
            content = choice.message.content
            usage = getattr(resp, "usage", None)
            if usage:
                print(f"[LLM] generate_join_reply: prompt_tokens={usage.prompt_tokens}, completion_tokens={usage.completion_tokens}, total_tokens={usage.total_tokens}")
            if not content:
                reason = getattr(choice, "finish_reason", "unknown")
                refusal = getattr(choice.message, "refusal", None)
                print(f"[LLM] generate_join_reply: content=None, finish_reason={reason}, refusal={refusal}")
                return ""
            return content.strip()
        except Exception as e:
            logger.error("generate_join_reply failed: %s", e)
            print(f"[LLM] generate_join_reply error: {e}")
            return ""

    async def close(self):
        await self.client.close()
