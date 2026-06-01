from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List

from config import Settings
from utils.logger import setup_logger

logger = setup_logger("event_handler")


@dataclass
class GroupMessageEvent:
    group_id: int
    user_id: int
    nickname: str
    message_id: int
    raw_text: str
    full_message: str
    segments: list
    is_at_bot: bool
    timestamp: int
    sender_role: str
    images: list = None  # list of image URLs
    has_sticker: bool = False

    def __post_init__(self):
        if self.images is None:
            self.images = []


@dataclass
class PrivateMessageEvent:
    user_id: int
    nickname: str
    message_id: int
    raw_text: str
    full_message: str
    segments: list
    timestamp: int
    images: list = None
    has_sticker: bool = False

    def __post_init__(self):
        if self.images is None:
            self.images = []


class EventHandler:
    def __init__(self, settings: Settings):
        self.bot_qq_id = settings.bot_qq_id
        self.target_group_ids = settings.group_ids

    def parse_group_message(self, data: dict) -> GroupMessageEvent | None:
        if data.get("post_type") != "message":
            return None
        if data.get("message_type") != "group":
            return None
        if self.target_group_ids and data.get("group_id") not in self.target_group_ids:
            return None

        segments = data.get("message", [])
        if not isinstance(segments, list):
            return None

        text_parts: list[str] = []
        full_parts: list[str] = []
        image_urls: list[str] = []
        is_at_bot = False
        has_sticker = False

        for seg in segments:
            seg_type = seg.get("type", "")
            if seg_type == "text":
                t = seg.get("data", {}).get("text", "")
                text_parts.append(t)
                full_parts.append(t)
            elif seg_type == "at":
                qq = seg.get("data", {}).get("qq", "")
                if str(qq) == str(self.bot_qq_id):
                    is_at_bot = True
                else:
                    # 非 bot 的 @ 也加入 raw_text，让 LLM 知道谁被 @ 了
                    at_name = seg.get("data", {}).get("name", str(qq))
                    text_parts.append(f"@{at_name}")
                full_parts.append(f"@{qq}")
            elif seg_type == "image":
                sub_type = seg.get("data", {}).get("subType", 0)
                url = seg.get("data", {}).get("url", "")
                if url:
                    image_urls.append(url)
                if sub_type == 1:
                    has_sticker = True
                    full_parts.append("[表情包]")
                else:
                    full_parts.append("[图片]")
            elif seg_type == "face":
                full_parts.append("[表情]")
            elif seg_type == "reply":
                continue
            else:
                full_parts.append(f"[{seg_type}]")

        sender = data.get("sender", {})
        nickname = (
            sender.get("card") or sender.get("nickname") or str(sender.get("user_id", ""))
        )
        role = sender.get("role", "member")

        event = GroupMessageEvent(
            group_id=data.get("group_id", 0),
            user_id=sender.get("user_id", 0),
            nickname=nickname,
            message_id=data.get("message_id", 0),
            raw_text="".join(text_parts).strip(),
            full_message="".join(full_parts).strip(),
            segments=segments,
            is_at_bot=is_at_bot,
            timestamp=data.get("time", 0),
            sender_role=role,
            images=image_urls,
            has_sticker="[表情包]" in "".join(full_parts),
        )

        logger.debug(
            "Parsed: [%s] %s (at_bot=%s)", event.nickname, event.raw_text[:50], is_at_bot
        )
        return event

    def parse_private_message(self, data: dict) -> PrivateMessageEvent | None:
        if data.get("post_type") != "message":
            return None
        if data.get("message_type") != "private":
            return None

        segments = data.get("message", [])
        if not isinstance(segments, list):
            return None

        text_parts: list[str] = []
        full_parts: list[str] = []
        image_urls: list[str] = []

        for seg in segments:
            seg_type = seg.get("type", "")
            if seg_type == "text":
                t = seg.get("data", {}).get("text", "")
                text_parts.append(t)
                full_parts.append(t)
            elif seg_type == "image":
                sub_type = seg.get("data", {}).get("subType", 0)
                url = seg.get("data", {}).get("url", "")
                if url:
                    image_urls.append(url)
                if sub_type == 1:
                    full_parts.append("[表情包]")
                else:
                    full_parts.append("[图片]")
            elif seg_type == "face":
                full_parts.append("[表情]")
            else:
                full_parts.append(f"[{seg_type}]")

        sender = data.get("sender", {})
        nickname = sender.get("nickname") or str(sender.get("user_id", ""))

        event = PrivateMessageEvent(
            user_id=sender.get("user_id", 0),
            nickname=nickname,
            message_id=data.get("message_id", 0),
            raw_text="".join(text_parts).strip(),
            full_message="".join(full_parts).strip(),
            segments=segments,
            timestamp=data.get("time", 0),
            images=image_urls,
            has_sticker="[表情包]" in "".join(full_parts),
        )

        logger.debug("Parsed private: [%s] %s", event.nickname, event.raw_text[:50])
        return event
