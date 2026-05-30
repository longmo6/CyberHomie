from __future__ import annotations

import json

from openai import AsyncOpenAI

from config import Settings
from utils.logger import setup_logger

logger = setup_logger("llm")


class LLMClient:
    def __init__(self, settings: Settings):
        self.client = AsyncOpenAI(
            api_key=settings.mimo_api_key,
            base_url=settings.mimo_base_url,
        )
        self.model = settings.mimo_model

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
        images: list[str] = None,
    ) -> str:
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(chat_history[-50:])
        messages.append({"role": "user", "content": self._build_content(user_message, images)})

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.85,
                top_p=0.9,
            )
            content = resp.choices[0].message.content
            return content.strip() if content else ""
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            return ""

    async def summarize_chat(self, messages: list[dict]) -> str:
        prompt = (
            "你是一个群聊观察者。请用2-3句话总结以下聊天记录的要点，"
            "包括谁说了什么重要的事、有什么梗或争论。用中文，随意一点。\n\n"
        )
        for msg in messages:
            sender = msg.get("nickname", "未知")
            content = msg.get("content", "")
            prompt += f"{sender}: {content}\n"

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
            )
            content = resp.choices[0].message.content
            return content.strip() if content else ""
        except Exception as e:
            logger.error("Summarize failed: %s", e)
            return ""

    async def analyze_user(self, nickname: str, messages: list[dict]) -> dict:
        prompt = (
            f"你是一个群聊观察者。根据以下{nickname}的聊天记录，分析这个人的特点。\n"
            "返回JSON格式：\n"
            '{"personality_notes": "性格特点", "interests": "兴趣话题", '
            '"emotional_tendency": "情绪倾向", "quirks": "怪癖或口头禅"}\n\n'
            f"聊天记录：\n"
        )
        for msg in messages:
            prompt += f"{msg.get('content', '')}\n"

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            return json.loads(content) if content else {}
        except Exception as e:
            logger.error("Analyze user failed: %s", e)
            return {}

    async def decide_replies(
        self,
        system_prompt: str,
        messages: list[dict],
        buffered: list[dict],
    ) -> list[dict]:
        """
        Given buffered messages, decide which to reply to.
        Returns list of {"message_id": int, "text": str, "quote": bool}
        """
        # Build message list, distinguishing stickers from regular images
        msg_list = ""
        all_images = []
        for m in buffered:
            at_tag = " [@bot]" if m.get("is_at_bot") else ""
            if m.get("has_sticker"):
                img_tag = " [表情包]"
            elif m.get("images"):
                img_tag = " [含图片]"
            else:
                img_tag = ""
            msg_list += f'[id={m["message_id"]}] {m["nickname"]}{at_tag}{img_tag}: {m["text"]}\n'
            all_images.extend(m.get("images", []))

        prompt = (
            "以下是你所在群聊最近的消息。你是群里的老群友，不是助手。\n"
            "请决定你要回复哪些消息。可以回复多条，也可以一条都不回。\n\n"
            "规则：\n"
            "- 被@你的消息必须回复\n"
            "- 有人对你说话、提到你、接你的话，都可以回\n"
            "- 你觉得有意思的、想参与的话题，可以回\n"
            "- 合理聊天范围内都可以回，不用刻意沉默\n"
            "- 只有明显和你无关的（别人@别人、别人之间的私聊）才不回\n"
            "- 被质疑是AI时不要死不承认，可以无语、反问、岔开话题\n"
            "- 如果有图片可以回应\n"
            "- 表情包不要较真分析，可以忽略、回语气词、或者不回\n"
            "- 如果你对这个人有记忆，可以自然融入语气，但不要说\"我记得\"\n\n"
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

        messages_for_llm = [{"role": "system", "content": system_prompt}]
        messages_for_llm.extend(messages[-10:])
        messages_for_llm.append({
            "role": "user",
            "content": self._build_content(prompt, all_images if all_images else None),
        })

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages_for_llm,
                temperature=0.85,
                top_p=0.9,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            if not content:
                return []
            data = json.loads(content)
            replies = data.get("replies", [])
            logger.debug("LLM decided %d replies", len(replies))
            return replies
        except Exception as e:
            logger.error("decide_replies failed: %s", e)
            return []

    async def generate_topic(
        self, system_prompt: str, recent_history: list[dict]
    ) -> str:
        """Generate a proactive conversation topic when chat has been quiet."""
        history_text = ""
        for msg in recent_history[-5:]:
            history_text += f"{msg.get('content', '')}\n"

        prompt = (
            "群聊已经安静了一会儿。你想找个话题聊聊天。\n"
            "根据之前的聊天内容和你对群的了解，随便说点什么开启话题。\n"
            "要求：\n"
            "- 像普通人突然想到什么一样自然地说出来\n"
            '- 不要太正式，不要"大家好"这种\n'
            "- 可以分享个有趣的事、问个问题、吐槽点什么\n"
            "- 一句话就行，不要长篇大论\n\n"
        )
        if history_text:
            prompt += f"之前的聊天：\n{history_text}\n\n"
        prompt += "你现在想说什么？直接说，不要任何前缀。"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
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
        self, system_prompt: str, recent_history: list[dict]
    ) -> str:
        """Generate a reply to join an ongoing conversation."""
        history_text = ""
        for msg in recent_history[-8:]:
            nickname = msg.get("nickname", "???")
            content = msg.get("content", "")
            history_text += f"{nickname}: {content}\n"

        prompt = (
            "群里正在聊天，你想插句话参与进去。\n"
            "根据最近的聊天内容，自然地接一句话融入对话。\n"
            "要求：\n"
            "- 像群友随意插嘴一样自然\n"
            "- 对刚才聊的内容发表看法、吐槽、补充、提问都行\n"
            "- 一句话就行，不要太长\n"
            "- 不要重复别人说过的话\n"
            "- 不要太正式，口语化\n\n"
        )
        if history_text:
            prompt += f"最近的聊天：\n{history_text}\n\n"
        prompt += "你现在想说什么？直接说，不要任何前缀。"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
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
