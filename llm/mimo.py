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

    async def generate_reply(
        self,
        system_prompt: str,
        chat_history: list[dict],
        user_message: str,
    ) -> str:
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(chat_history[-10:])
        messages.append({"role": "user", "content": user_message})

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=200,
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
                max_tokens=300,
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
                max_tokens=300,
                temperature=0.7,
                response_format={"type": "json_object"},
            )
            import json
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
        msg_list = ""
        for m in buffered:
            at_tag = " [@bot]" if m.get("is_at_bot") else ""
            msg_list += f'[id={m["message_id"]}] {m["nickname"]}{at_tag}: {m["text"]}\n'

        prompt = (
            "以下是你所在群聊最近的消息。你是群里的老群友，性格温和软糯，不是助手。\n"
            "请决定你要回复哪些消息。可以回复多条，也可以一条都不回。\n\n"
            "核心规则：\n"
            "- 被@你的消息必须回复\n"
            "- 有人直接对你说话（叫你名字、接着你的话说）可以回复\n"
            "- 你感兴趣的话题可以偶尔插嘴，但不要抢话\n"
            "- 别人之间聊天不要插嘴，那不是跟你说的\n"
            "- 别人@了其他人的问题不要回答，那是问别人的\n"
            "- 不要每条都回，安静潜水是常态\n"
            "- 性格温和，不要骂人不要攻击\n"
            "- 被质疑是AI时要装傻\n\n"
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
        messages_for_llm.append({"role": "user", "content": prompt})

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages_for_llm,
                max_tokens=500,
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

    async def close(self):
        await self.client.close()
