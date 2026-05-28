"""
Humanizer - 消息缓冲 + 参与度管理 + 随机出没调度

核心机制：
1. 消息不是立即回复，而是先缓冲，定期批量交给 LLM 决策
2. 每个群有独立的参与度（0-100），从 @-mention 的 100 衰减到 0（约3分钟）
3. 随机出没：bot 会随机"上线"一段时间参与聊天
4. 深夜（0-8点）更活跃：出没间隔短、活跃时间长
   白天（8-0点）更低调：出没间隔长、活跃时间短
"""
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

# --- 评估触发条件 ---
ENGAGE_EVAL_INTERVAL = 10.0    # 每 10 秒评估一次
ENGAGE_EVAL_MSG_COUNT = 3      # 或每 3 条消息评估一次
ENGAGE_DECAY_DURATION = 180.0  # 参与度从 100 衰减到 0 的秒数（3分钟）

# --- 深夜出没参数（0:00-8:00）---
NIGHT_GAP_MIN = 10             # 出没间隔最小（分钟）
NIGHT_GAP_MAX = 30             # 出没间隔最大（分钟）
NIGHT_DURATION_MIN = 5         # 每次出没最短（分钟）
NIGHT_DURATION_MAX = 15        # 每次出没最长（分钟）
NIGHT_ENGAGEMENT = 50          # 出没时初始参与度

# --- 白天出没参数（8:00-0:00）---
DAY_GAP_MIN = 40               # 出没间隔最小（分钟）
DAY_GAP_MAX = 90               # 出没间隔最大（分钟）
DAY_DURATION_MIN = 2           # 每次出没最短（分钟）
DAY_DURATION_MAX = 5           # 每次出没最长（分钟）
DAY_ENGAGEMENT = 30            # 出没时初始参与度


@dataclass
class BufferedMessage:
    """缓冲区中的单条消息"""
    message_id: int
    user_id: int
    nickname: str
    text: str
    is_at_bot: bool
    group_id: int = 0
    images: List[str] = field(default_factory=list)  # image URLs
    timestamp: float = field(default_factory=time.time)


@dataclass
class GroupState:
    """每个群独立的状态：参与度、活跃期、缓冲区"""
    engagement: float = 0.0           # 当前参与度 0-100
    engage_set_time: float = 0.0      # 参与度设定时间（用于计算衰减）
    last_eval_time: float = 0.0       # 上次评估时间
    messages_since_eval: int = 0      # 上次评估后的消息数
    session_active_until: Optional[float] = None  # 活跃期结束时间戳
    next_session_time: float = 0.0    # 下次随机出没的时间戳
    buffer: List[BufferedMessage] = field(default_factory=list)  # 消息缓冲区
    eval_task: Optional[asyncio.Task] = None  # 定时评估任务


class Humanizer:
    def __init__(self, settings: Settings):
        self.active_hour_start = settings.active_hour_start
        self.active_hour_end = settings.active_hour_end

        # 每群独立状态（group_id -> GroupState）
        self._groups: Dict[int, GroupState] = {}

        # 回调函数
        self._on_session_end: Optional[Callable[[int], Awaitable[None]]] = None
        self._on_flush: Optional[Callable[[int, List[BufferedMessage], float], Awaitable[None]]] = None

    def _get_state(self, group_id: int) -> GroupState:
        """获取或创建群状态"""
        if group_id not in self._groups:
            state = GroupState()
            self._schedule_next_session(state)
            self._groups[group_id] = state
        return self._groups[group_id]

    def set_session_end_callback(self, cb: Callable[[int], Awaitable[None]]):
        self._on_session_end = cb

    def set_flush_callback(self, cb: Callable[[int, List[BufferedMessage], float], Awaitable[None]]):
        self._on_flush = cb

    # ============================================================
    # 参与度管理
    # ============================================================

    def trigger_active(self, group_id: int, minutes: int = 3, engagement: float = 100):
        """外部触发活跃状态（如被 @-mention）"""
        state = self._get_state(group_id)
        state.engagement = engagement
        state.engage_set_time = time.time()
        until = time.time() + minutes * 60
        if state.session_active_until is None or until > state.session_active_until:
            state.session_active_until = until
        logger.info("[Group %d] Engagement %.0f for %d min", group_id, engagement, minutes)

    def get_current_engagement(self, group_id: int) -> float:
        """计算当前参与度（含时间衰减）"""
        state = self._get_state(group_id)
        if state.engagement <= 0:
            return 0.0
        elapsed = time.time() - state.engage_set_time
        decay = elapsed / ENGAGE_DECAY_DURATION * 100
        return max(0.0, state.engagement - decay)

    def notify_bot_replied(self, group_id: int):
        """bot 回复后提升参与度，保持对话连贯"""
        state = self._get_state(group_id)
        current = self.get_current_engagement(group_id)
        state.engagement = min(100, current + 20)
        state.engage_set_time = time.time()

    # ============================================================
    # 消息缓冲 + 评估
    # ============================================================

    async def buffer_message(self, event: GroupMessageEvent) -> Optional[List[BufferedMessage]]:
        """
        缓冲消息。返回 None 表示已缓冲等待后续处理。
        返回 list 表示需要立即处理（@-mention 触发）。
        """
        gid = event.group_id
        state = self._get_state(gid)
        msg = BufferedMessage(
            message_id=event.message_id,
            user_id=event.user_id,
            nickname=event.nickname,
            text=event.raw_text,
            is_at_bot=event.is_at_bot,
            group_id=gid,
            images=event.images or [],
        )

        # @-mention：立即处理，触发 3 分钟高活跃
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

        # 普通消息：加入缓冲区
        state.buffer.append(msg)
        state.messages_since_eval += 1

        # 检查随机出没状态
        self._check_random_session(gid)

        # 检查是否该评估了（10秒 或 3条消息）
        if self._should_evaluate(state):
            return await self._do_evaluate(gid, state)

        # 安排定时评估
        if state.eval_task is None or state.eval_task.done():
            state.eval_task = asyncio.create_task(self._timed_eval(gid))

        return None

    def _should_evaluate(self, state: GroupState) -> bool:
        """是否应该立即评估缓冲区"""
        if not state.buffer:
            return False
        if time.time() - state.last_eval_time >= ENGAGE_EVAL_INTERVAL:
            return True
        if state.messages_since_eval >= ENGAGE_EVAL_MSG_COUNT:
            return True
        return False

    async def _do_evaluate(self, group_id: int, state: GroupState) -> List[BufferedMessage]:
        """取出缓冲区消息用于评估"""
        messages = state.buffer.copy()
        state.buffer.clear()
        state.messages_since_eval = 0
        state.last_eval_time = time.time()
        return messages

    async def _timed_eval(self, group_id: int):
        """定时评估：等待一段时间后触发 flush"""
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

    # ============================================================
    # 随机出没调度
    # ============================================================

    def _is_night(self) -> bool:
        """是否为深夜时段（0:00-8:00）"""
        hour = datetime.now().hour
        return 0 <= hour < 8

    def _get_session_params(self) -> tuple:
        """
        根据当前时间返回出没参数：
        深夜：间隔短、活跃久、参与度高
        白天：间隔长、活跃短、参与度低
        """
        if self._is_night():
            return (
                NIGHT_GAP_MIN, NIGHT_GAP_MAX,
                NIGHT_DURATION_MIN, NIGHT_DURATION_MAX,
                NIGHT_ENGAGEMENT,
            )
        return (
            DAY_GAP_MIN, DAY_GAP_MAX,
            DAY_DURATION_MIN, DAY_DURATION_MAX,
            DAY_ENGAGEMENT,
        )

    def _schedule_next_session(self, state: GroupState):
        """安排下一次随机出没时间"""
        gap_min, gap_max, _, _, _ = self._get_session_params()
        gap = random.randint(gap_min, gap_max)
        state.next_session_time = time.time() + gap * 60
        logger.debug("Next session in %d min", gap)

    def _check_random_session(self, group_id: int):
        """
        检查随机出没状态：
        1. 活跃期中 → 继续
        2. 活跃期刚结束 → 清空缓冲、触发总结
        3. 到了出没时间 → 开始新的出没
        """
        state = self._get_state(group_id)
        now = time.time()

        # 活跃期中
        if state.session_active_until and now < state.session_active_until:
            return True

        # 活跃期刚结束
        if state.session_active_until and now >= state.session_active_until:
            state.session_active_until = None
            state.buffer.clear()
            state.engagement = 0
            state.messages_since_eval = 0
            self._schedule_next_session(state)
            logger.info("[Group %d] Session ended", group_id)
            if self._on_session_end:
                asyncio.ensure_future(self._on_session_end(group_id))
            return False

        # 到了出没时间
        if state.next_session_time > 0 and now >= state.next_session_time:
            if self._is_active_hour():
                _, _, dur_min, dur_max, engage = self._get_session_params()
                duration = random.randint(dur_min, dur_max)
                state.session_active_until = now + duration * 60
                state.engagement = engage
                state.engage_set_time = now
                logger.info("[Group %d] Session started, %d min (engagement=%d)",
                            group_id, duration, engage)
                return True
            else:
                # 非活跃时段，跳过这次
                self._schedule_next_session(state)
                return False

        return False

    def _is_active_hour(self) -> bool:
        """是否在配置的活跃时段内"""
        hour = datetime.now().hour
        if self.active_hour_start <= self.active_hour_end:
            return self.active_hour_start <= hour < self.active_hour_end
        return hour >= self.active_hour_start or hour < self.active_hour_end

    # ============================================================
    # 状态查询 + 后处理
    # ============================================================

    def get_session_status(self, group_id: int = 0) -> str:
        if group_id:
            eng = self.get_current_engagement(group_id)
            state = self._get_state(group_id)
            buf_len = len(state.buffer)
            night = " [深夜模式]" if self._is_night() else ""
            if eng > 50:
                return f"ACTIVE (engagement={eng:.0f}, buffer={buf_len}){night}"
            if eng > 0:
                return f"Cooling (engagement={eng:.0f}, buffer={buf_len}){night}"
            if state.session_active_until and time.time() < state.session_active_until:
                return f"Random session (buffer={buf_len}){night}"
            if state.next_session_time:
                gap = int(state.next_session_time - time.time())
                return f"IDLE (next in {gap // 60}min, buffer={buf_len}){night}"
            return f"IDLE (buffer={buf_len}){night}"
        lines = []
        for gid in self._groups:
            lines.append(f"  Group {gid}: {self.get_session_status(gid)}")
        return "\n".join(lines) if lines else "No groups active"

    def get_all_group_ids(self) -> List[int]:
        return list(self._groups.keys())

    def post_process_reply(self, text: str) -> str:
        """回复后处理：去除AI痕迹、加语气词、截断"""
        if not text:
            return text
        for pattern in FORMAL_PATTERNS:
            text = re.sub(pattern, "", text)
        text = text.strip()
        # 10% 概率加语气词
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
