from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Awaitable, Optional, List

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

# Engagement thresholds
ENGAGE_EVAL_INTERVAL = 10.0    # seconds between evaluations
ENGAGE_EVAL_MSG_COUNT = 3      # or after this many messages
ENGAGE_DECAY_DURATION = 180.0  # seconds for engagement to decay from 100 to 0


@dataclass
class BufferedMessage:
    message_id: int
    user_id: int
    nickname: str
    text: str
    is_at_bot: bool
    timestamp: float = field(default_factory=time.time)


class Humanizer:
    def __init__(self, settings: Settings):
        self.active_hour_start = settings.active_hour_start
        self.active_hour_end = settings.active_hour_end
        self.session_gap_min = settings.session_gap_min
        self.session_gap_max = settings.session_gap_max
        self.session_duration_min = settings.session_duration_min
        self.session_duration_max = settings.session_duration_max

        # Engagement state
        self._engagement: float = 0.0          # 0-100
        self._engage_set_time: float = 0.0     # when engagement was last set
        self._last_eval_time: float = 0.0      # last evaluation timestamp
        self._messages_since_eval: int = 0

        # Random session state
        self._session_active_until: Optional[float] = None
        self._next_session_time: Optional[float] = None
        self._schedule_next_session()

        # Callbacks
        self._on_session_end: Optional[Callable[[], Awaitable[None]]] = None
        self._on_flush: Optional[Callable[[List[BufferedMessage], float], Awaitable[None]]] = None

        # Buffer
        self._buffer: List[BufferedMessage] = []
        self._eval_task: Optional[asyncio.Task] = None

    # --- Public API ---

    def set_session_end_callback(self, cb: Callable[[], Awaitable[None]]):
        self._on_session_end = cb

    def set_flush_callback(self, cb: Callable[[List[BufferedMessage], float], Awaitable[None]]):
        """Callback receives (messages, engagement_level)."""
        self._on_flush = cb

    def trigger_active(self, minutes: int = 3, engagement: float = 100):
        """Trigger active state (e.g. from @-mention)."""
        self._engagement = engagement
        self._engage_set_time = time.time()
        until = time.time() + minutes * 60
        if self._session_active_until is None or until > self._session_active_until:
            self._session_active_until = until
        logger.info("Engagement set to %.0f for %d min", engagement, minutes)

    def get_current_engagement(self) -> float:
        """Get current engagement level with decay applied."""
        if self._engagement <= 0:
            return 0.0
        elapsed = time.time() - self._engage_set_time
        decay = elapsed / ENGAGE_DECAY_DURATION * 100
        return max(0.0, self._engagement - decay)

    async def buffer_message(self, event: GroupMessageEvent) -> Optional[List[BufferedMessage]]:
        """
        Buffer a message. Returns messages to process immediately for @-mention,
        or None if buffered for later evaluation.
        """
        msg = BufferedMessage(
            message_id=event.message_id,
            user_id=event.user_id,
            nickname=event.nickname,
            text=event.raw_text,
            is_at_bot=event.is_at_bot,
        )

        # @-mention: immediate evaluation with high engagement
        if event.is_at_bot:
            self.trigger_active(3, engagement=100)
            # Cancel pending eval, flush now
            if self._eval_task and not self._eval_task.done():
                self._eval_task.cancel()
            pending = self._buffer.copy()
            self._buffer.clear()
            pending.append(msg)
            self._messages_since_eval = 0
            self._last_eval_time = time.time()
            return pending

        # Add to buffer
        self._buffer.append(msg)
        self._messages_since_eval += 1

        # Check random session state
        self._check_random_session()

        # Check if it's time to evaluate
        if self._should_evaluate():
            return await self._do_evaluate()

        # Schedule evaluation if not already scheduled
        if self._eval_task is None or self._eval_task.done():
            self._eval_task = asyncio.create_task(self._timed_eval())

        return None

    def notify_bot_replied(self):
        """Boost engagement slightly when bot replies (keeps conversation going)."""
        current = self.get_current_engagement()
        boost = min(100, current + 20)
        self._engagement = boost
        self._engage_set_time = time.time()

    def get_session_status(self) -> str:
        eng = self.get_current_engagement()
        if eng > 50:
            return f"ACTIVE (engagement={eng:.0f})"
        if eng > 0:
            return f"Cooling down (engagement={eng:.0f})"
        if self._session_active_until and time.time() < self._session_active_until:
            return "Random session active"
        if self._next_session_time:
            gap = int(self._next_session_time - time.time())
            return f"IDLE (next session in {gap // 60}min)"
        return "IDLE"

    # --- Internal ---

    def _should_evaluate(self) -> bool:
        """Check if we should evaluate the buffer now."""
        if not self._buffer:
            return False
        # Time-based: 10 seconds since last eval
        if time.time() - self._last_eval_time >= ENGAGE_EVAL_INTERVAL:
            return True
        # Count-based: 3+ messages since last eval
        if self._messages_since_eval >= ENGAGE_EVAL_MSG_COUNT:
            return True
        return False

    async def _do_evaluate(self) -> List[BufferedMessage]:
        """Flush buffer for evaluation."""
        messages = self._buffer.copy()
        self._buffer.clear()
        self._messages_since_eval = 0
        self._last_eval_time = time.time()
        logger.debug("Evaluating %d messages (engagement=%.0f)",
                      len(messages), self.get_current_engagement())
        return messages

    async def _timed_eval(self):
        """Wait until next evaluation point, then flush."""
        try:
            await asyncio.sleep(ENGAGE_EVAL_INTERVAL)
        except asyncio.CancelledError:
            return

        if self._buffer:
            messages = await self._do_evaluate()
            if self._on_flush and messages:
                engagement = self.get_current_engagement()
                await self._on_flush(messages, engagement)

    def _schedule_next_session(self):
        gap = random.randint(self.session_gap_min, self.session_gap_max)
        self._next_session_time = time.time() + gap * 60

    def _check_random_session(self) -> bool:
        """Check and manage random session state."""
        now = time.time()
        if self._session_active_until and now < self._session_active_until:
            return True
        if self._session_active_until and now >= self._session_active_until:
            self._session_active_until = None
            self._schedule_next_session()
            # Session ended: clear buffer and reset engagement
            self._buffer.clear()
            self._engagement = 0
            self._messages_since_eval = 0
            logger.info("Random session ended")
            if self._on_session_end:
                asyncio.ensure_future(self._on_session_end())
            return False
        if self._next_session_time and now >= self._next_session_time:
            if self._is_active_hour():
                duration = random.randint(self.session_duration_min, self.session_duration_max)
                self._session_active_until = now + duration * 60
                self._engagement = 40
                self._engage_set_time = now
                logger.info("Random session started, %d min", duration)
                return True
            else:
                self._schedule_next_session()
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
