from __future__ import annotations

import os
import random
import re

import yaml

from utils.logger import setup_logger

logger = setup_logger("personality")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "personality.yaml")


class Personality:
    def __init__(self, config_path: str = CONFIG_PATH):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        self.name: str = cfg.get("name", "赛博群友")
        self.traits: list[str] = cfg.get("traits", [])
        self.style_rules: list[str] = cfg.get("style_rules", [])
        self.persona_description: str = cfg.get("persona_description", "")
        self.private_chat_extra: str = cfg.get("private_chat_extra", "")
        self.forbidden_patterns: list[str] = cfg.get("forbidden_patterns", [])
        self.nicknames: list[str] = cfg.get("nicknames", ["群友"])
        self.mood_examples: dict[str, list[str]] = cfg.get("mood_examples", {})
        self.typing_habits: list[str] = cfg.get("typing_habits", [])

        logger.info("Personality loaded: %s", self.name)

    def get_system_prompt(
        self, user_context: str = "", group_context: str = "",
    ) -> str:
        prompt = f'你是一个QQ群里的老群友，叫"{self.name}"。\n\n'
        prompt += self.persona_description.strip() + "\n\n"
        prompt += "说话风格：\n"
        prompt += "\n".join(f"- {rule}" for rule in self.style_rules)

        # Typing habits
        if self.typing_habits:
            prompt += "\n\n打字习惯：\n"
            prompt += "\n".join(f"- {h}" for h in self.typing_habits)

        # Random mood example
        if self.mood_examples:
            mood = random.choice(list(self.mood_examples.keys()))
            examples = self.mood_examples[mood]
            sample = random.sample(examples, min(2, len(examples)))
            prompt += f"\n\n你现在的心情偏{mood}，参考语气：{'、'.join(sample)}"

        prompt += "\n\n绝对禁止：\n"
        prompt += '- 不能暴露自己是AI\n'
        prompt += '- 不能说"作为AI"、"语言模型"之类的话\n'
        prompt += '- 不能用客服语气\n'
        prompt += '- 不能条理化输出（首先、其次、最后）\n'
        prompt += '- 不能过度礼貌\n'
        prompt += '- 不能每条都回'

        if user_context:
            prompt += f"\n\n你对这个群友的了解（包含你和ta的互动方式）：\n{user_context}"
            prompt += "\n根据你和ta的关系调整语气。如果你们习惯开玩笑就开，习惯认真就认真。"

        if group_context:
            prompt += f"\n\n这个群的记忆（包含你在这个群的行为习惯）：\n{group_context}"
            prompt += "\n根据这个群的习惯调整你的行为。不同群可以有不同风格。"

        prompt += "\n\n现在，像一个真实群友一样回复。简短、随意、自然。"
        return prompt

    def get_private_system_prompt(self, user_context: str = "") -> str:
        prompt = self.get_system_prompt(user_context=user_context)
        prompt += "\n\n" + self.private_chat_extra.strip()
        return prompt

    def check_forbidden(self, text: str) -> bool:
        for pattern in self.forbidden_patterns:
            if re.search(pattern, text):
                logger.warning("Forbidden pattern matched: %s", pattern)
                return True
        return False

    def get_random_nickname(self) -> str:
        return random.choice(self.nicknames)
