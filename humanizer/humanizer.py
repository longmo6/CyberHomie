from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Awaitable, Optional, List, Dict

from config import Settings
from core.event_handler import GroupMessageEvent
from utils.logger import setup_logger

logger = setup_logger("humanizer")

FILLER_WORDS = ["啊", "吧", "呢", "嘛", "嗯", "哦", "额", "呃"]
FORMAL_PATTERNS = [
    r"作为.{0,5}(助手|AI|人工智能)",
    r"我(无法|不能|不可以)",
    r"请注意",
    r"需要(注意|说明)的是",
    r"以下(是|为)",
    r"首先.{0,3}其次",
    r"综上所述",
    r"希望(对)?你(有)?帮助",
]

ENGAGE_EVAL_INTERVAL = 10.0
ENGAGE_EVAL_MSG_COUNT = 3
ENGAGE_DECAY_DURATION = 180.0


@dataclass
class BufferedMessage:
    message_id: int
    user_id: int
    nickname: str
    text: str
    is_at_bot: bool
    group_id: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class GroupState:
    """Per-group engagement and session state."""
    engagement: float = 0.0
    engage_set_time: float = 0.0
    last_eval_time: float = 0.0
    messages_since_eval: int = 0
    session_active_until: Optional[float] = None
    next_session_time: float = 0.0
    buffer: List[BufferedMessage] = field(default_factory=list)
    eval_task: Optional[asyncio.Task] = None


class Humanizer:
    def __init__(self, settings: Settings):
        self.active_hour_start = settings.active_hour_start
        self.active_hour_end = settings.active_hour_end
        self.session_gap_min = settings.session_gap_min
        self.session_gap_max = settings.session_gap_max
        self.session_duration_min = settings.session_duration_min
        self.session_duration_max = settings.session_duration_max

        self._groups: Dict[int, GroupState] = {}
        self._on_session_end: Optional[Callable[[int], Awaitable[None]]] = None
        self._on_flush: Optional[Callable[[int, List[BufferedMessage], float], Awaitable[None]]] = None

    def _get_state(self, group_id: int) -> GroupState:
        if group_id not in self._groups:
            state = GroupState()
            self._schedule_next_session(state)
            self._groups[group_id] = state
        return self._groups[group_id]

    def set_session_end_callback(self, cb: Callable[[int], Awaitable[None]]):
        """Callback receives group_id."""
        self._on_session_end = cb

    def set_flush_callback(self, cb: Callable[[int, List[BufferedMessage], float], Awaitable[None]]):
        """Callback receives (group_id, messages, engagement_level)."""
        self._on_flush = cb

    def trigger_active(self, group_id: int, minutes: int = 3, engagement: float = 100):
        state = self._get_state(group_id)
        state.engagement = engagement
        state.engage_set_time = time.time()
        until = time.time() + minutes * 60
        if state.session_active_until is None or until > state.session_active_until:
            state.session_active_until = until
        logger.info("[Group %d] Engagement %.0f for %d min", group_id, engagement, minutes)

    def get_current_engagement(self, group_id: int) -> float:
        state = self._get_state(group_id)
        if state.engagement <= 0:
            return 0.0
        elapsed = time.time() - state.engage_set_time
        decay = elapsed / ENGAGE_DECAY_DURATION * 100
        return max(0.0, state.engagement - decay)

    async def buffer_message(self, event: GroupMessageEvent) -> Optional[List[BufferedMessage]]:
        gid = event.group_id
        state = self._get_state(gid)
        msg = BufferedMessage(
            message_id=event.message_id,
            user_id=event.user_id,
            nickname=event.nickname,
            text=event.raw_text,
            is_at_bot=event.is_at_bot,
            group_id=gid,
        )

        # @-mention: immediate
        if event.is_at_bot:
            self.trigger_active(gid, 3, 100)
            if state.eval_task and not state.eval_task.done():
                state.eval_task.cancel()
            pending = state.buffer.copy()
            state.buffer.clear()
            pending.append(msg)
            state.messages_since_eval = 0
            state.last_eval_time = time.time()
            return pending

        state.buffer.append(msg)
        state.messages_since_eval += 1

        self._check_random_session(gid)

        if self._should_evaluate(state):
            return await self._do_evaluate(gid, state)

        if state.eval_task is None or state.eval_task.done():
            state.eval_task = asyncio.create_task(self._timed_eval(gid))

        return None

    def notify_bot_replied(self, group_id: int):
        state = self._get_state(group_id)
        current = self.get_current_engagement(group_id)
        state.engagement = min(100, current + 20)
        state.engage_set_time = time.time()

    def get_session_status(self, group_id: int = 0) -> str:
        if group_id:
            eng = self.get_current_engagement(group_id)
            state = self._get_state(group_id)
            buf_len = len(state.buffer)
            if eng > 50:
                return f"ACTIVE (engagement={eng:.0f}, buffer={buf_len})"
            if eng > 0:
                return f"Cooling (engagement={eng:.0f}, buffer={buf_len})"
            if state.session_active_until and time.time() < state.session_active_until:
                return f"Random session (buffer={buf_len})"
            if state.next_session_time:
                gap = int(state.next_session_time - time.time())
                return f"IDLE (next in {gap // 60}min, buffer={buf_len})"
            return f"IDLE (buffer={buf_len})"
        # All groups
        lines = []
        for gid in self._groups:
            lines.append(f"  Group {gid}: {self.get_session_status(gid)}")
        return "\n".join(lines) if lines else "No groups active"

    def get_all_group_ids(self) -> List[int]:
        return list(self._groups.keys())

    # --- Internal ---

    def _should_evaluate(self, state: GroupState) -> bool:
        if not state.buffer:
            return False
        if time.time() - state.last_eval_time >= ENGAGE_EVAL_INTERVAL:
            return True
        if state.messages_since_eval >= ENGAGE_EVAL_MSG_COUNT:
            return True
        return False

    async def _do_evaluate(self, group_id: int, state: GroupState) -> List[BufferedMessage]:
        messages = state.buffer.copy()
        state.buffer.clear()
        state.messages_since_eval = 0
        state.last_eval_time = time.time()
        return messages

    async def _timed_eval(self, group_id: int):
        try:
            await asyncio.sleep(ENGAGE_EVAL_INTERVAL)
        except asyncio.CancelledError:
            return
        state = self._get_state(group_id)
        if state.buffer:
            messages = await self._do_evaluate(group_id, state)
            if self._on_flush and messages:
                engagement = self.get_current_engagement(group_id)
                await self._on_flush(group_id, messages, engagement)

    def _schedule_next_session(self, state: GroupState):
        gap = random.randint(self.session_gap_min, self.session_gap_max)
        state.next_session_time = time.time() + gap * 60

    def _check_random_session(self, group_id: int):
        state = self._get_state(group_id)
        now = time.time()
        if state.session_active_until and now < state.session_active_until:
            return True
        if state.session_active_until and now >= state.session_active_until:
            state.session_active_until = None
            state.buffer.clear()
            state.engagement = 0
            state.messages_since_eval = 0
            self._schedule_next_session(state)
            logger.info("[Group %d] Random session ended", group_id)
            if self._on_session_end:
                asyncio.ensure_future(self._on_session_end(group_id))
            return False
        if state.next_session_time > 0 and now >= state.next_session_time:
            if self._is_active_hour():
                duration = random.randint(self.session_duration_min, self.session_duration_max)
                state.session_active_until = now + duration * 60
                state.engagement = 40
                state.engage_set_time = now
                logger.info("[Group %d] Random session started, %d min", group_id, duration)
                return True
            else:
                self._schedule_next_session(state)
                return False
        return False

    def _is_active_hour(self) -> bool:
        hour = datetime.now().hour
        if self.active_hour_start <= self.active_hour_end:
            return self.active_hour_start <= hour < self.active_hour_end
        return hour >= self.active_hour_start or hour < self.active_hour_end

    def post_process_reply(self, text: str) -> str:
        if not text:
            return text
        for pattern in FORMAL_PATTERNS:
            text = re.sub(pattern, "", text)
        text = text.strip()
        if random.random() < 0.10 and text:
            filler = random.choice(FILLER_WORDS)
            if text[-1] in ("。", "！", "？", ".", "!", "?"):
                text = text[:-1] + filler + text[-1]
            else:
                text = text + filler
        if len(text) > 300:
            text = text[:280] + "..."
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
