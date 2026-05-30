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
from llm.mimo import ConversationSession
from utils.logger import setup_logger

logger = setup_logger("humanizer")

FILLER_WORDS = ["啊", "吧", "呢", "嘛", "嗯", "哦", "额", "呃"]

# API 错误/风控信息，绝不能发出去
REJECT_PATTERNS = [
    r"request was rejected",
    r"high risk",
    r"content.?filter",
    r"safety.?system",
    r"rate.?limit",
    r"quota.?exceed",
    r"invalid.?request",
    r"blocked",
    r"violation",
]
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

# --- 参与度衰减 ---
ENGAGE_DECAY_DURATION = 300.0  # 参与度从 100 衰减到 0 的秒数（5分钟）

# --- 深夜出没参数（0:00-8:00）---
NIGHT_GAP_MIN = 10             # 出没间隔最小（分钟）
NIGHT_GAP_MAX = 30             # 出没间隔最大（分钟）
NIGHT_ENGAGEMENT = 50          # 出没时初始参与度

# --- 白天出没参数（8:00-0:00）---
DAY_GAP_MIN = 40               # 出没间隔最小（分钟）
DAY_GAP_MAX = 90               # 出没间隔最大（分钟）
DAY_ENGAGEMENT = 30            # 出没时初始参与度

# --- 保底回复间隔（秒）---
GUARANTEED_REPLY_INTERVAL = 40.0


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
    has_sticker: bool = False  # 是否包含表情包
    timestamp: float = field(default_factory=time.time)


@dataclass
class GroupState:
    """每个群独立的状态：参与度、缓冲区"""
    engagement: float = 0.0           # 当前参与度 0-100，>0就是活跃
    engage_set_time: float = 0.0      # 参与度设定时间（用于计算衰减）
    next_session_time: float = 0.0    # 下次随机出没的时间戳
    buffer: List[BufferedMessage] = field(default_factory=list)  # 消息缓冲区
    fatigue: float = 0.0              # 疲惫值 0-100，越高越敷衍
    fatigue_set_time: float = 0.0     # 疲惫值设定时间（独立衰减）
    reply_count: int = 0              # 本轮已回复次数
    at_count: int = 0                 # 连续被@次数（session内）
    at_charges: int = 2              # 沉默期@回复机会（上限2，每15分钟恢复1）
    at_last_recharge: float = 0.0    # 上次恢复时间
    active_users: set = field(default_factory=set)  # 本轮被回复过的用户ID
    session: Optional[ConversationSession] = None  # session 内滚动对话窗口
    last_reply_time: float = 0.0    # 上次回复时间戳（用于保底回复）


class Humanizer:
    def __init__(self, settings: Settings):
        self.active_hour_start = settings.active_hour_start
        self.active_hour_end = settings.active_hour_end

        # 每群独立状态（group_id -> GroupState）
        self._groups: Dict[int, GroupState] = {}
        # 预创建所有群的状态
        for gid in settings.group_ids:
            self._get_state(gid)

        # 回调函数
        self._on_session_start: Optional[Callable[[int], Awaitable[None]]] = None
        self._on_session_end: Optional[Callable[[int], Awaitable[None]]] = None

    def _get_state(self, group_id: int) -> GroupState:
        """获取或创建群状态"""
        if group_id not in self._groups:
            state = GroupState()
            self._schedule_next_session(state)
            self._groups[group_id] = state
        return self._groups[group_id]

    def set_session_start_callback(self, cb: Callable[[int], Awaitable[None]]):
        self._on_session_start = cb

    def set_session_end_callback(self, cb: Callable[[int], Awaitable[None]]):
        self._on_session_end = cb

    # ============================================================
    # 参与度管理
    # ============================================================

    def trigger_active(self, group_id: int, engagement: float = 100, force: bool = False):
        """设置参与度。force=True 时强制重置（随机出没），否则只在空闲时设置（@-mention）。"""
        state = self._get_state(group_id)
        if not force and self.is_active(group_id):
            return  # 活跃状态下不再重置参与度
        state.engagement = engagement
        state.engage_set_time = time.time()
        logger.info("[Group %d] Engagement set to %.0f", group_id, engagement)

    def is_active(self, group_id: int) -> bool:
        """参与度 > 0 就是活跃"""
        return self.get_current_engagement(group_id) > 0

    def get_current_engagement(self, group_id: int) -> float:
        """计算当前参与度（含时间衰减）"""
        state = self._get_state(group_id)
        if state.engagement <= 0:
            return 0.0
        elapsed = time.time() - state.engage_set_time
        decay = elapsed / ENGAGE_DECAY_DURATION * 100
        return max(0.0, state.engagement - decay)

    def notify_bot_replied(self, group_id: int, was_at: bool = False):
        """bot 回复后累积疲惫值。"""
        state = self._get_state(group_id)
        state.reply_count += 1
        state.fatigue = min(100, state.fatigue + 5 + state.reply_count)
        state.fatigue_set_time = time.time()
        state.last_reply_time = time.time()
        if was_at:
            state.at_count += 1

    def should_reply_to_at(self, group_id: int) -> bool:
        """判断是否回复 @-mention。
        活跃期：连续 @ 次数越多，忽略概率越高。
        沉默期：实时充能系统，上限2，每15分钟恢复1。
        """
        state = self._get_state(group_id)
        now = time.time()

        # 活跃期：@疲劳概率
        if self.is_active(group_id):
            at = state.at_count
            if at <= 5:
                return True
            elif at <= 10:
                return random.random() < 0.8
            elif at <= 15:
                return random.random() < 0.5
            else:
                return random.random() < 0.3

        # 沉默期：先恢复充能
        if state.at_last_recharge > 0:
            elapsed = now - state.at_last_recharge
            gained = int(elapsed / 900)  # 每15分钟恢复1
            if gained > 0:
                state.at_charges = min(2, state.at_charges + gained)
                state.at_last_recharge = now
        else:
            state.at_last_recharge = now

        if state.at_charges <= 0:
            logger.info("[Group %d] @ no charges left (0/2)", group_id)
            return False

        # 消耗1次
        state.at_charges -= 1
        state.at_last_recharge = now
        logger.info("[Group %d] @ reply (%d/2 charges left)", group_id, state.at_charges)
        return True

    def record_replied_user(self, group_id: int, user_id: int):
        """记录本轮被bot回复过的用户（只有真正互动过的人）"""
        state = self._get_state(group_id)
        state.active_users.add(user_id)

    def get_replied_users(self, group_id: int) -> set:
        """获取本轮被回复过的用户"""
        state = self._get_state(group_id)
        return state.active_users.copy()

    def get_fatigue(self, group_id: int) -> float:
        """获取当前疲惫值（含时间衰减：每10秒减1）"""
        state = self._get_state(group_id)
        if state.fatigue <= 0:
            return 0.0
        if state.fatigue_set_time > 0:
            elapsed = time.time() - state.fatigue_set_time
            decay = elapsed / 10
            state.fatigue = max(0.0, state.fatigue - decay)
            state.fatigue_set_time = time.time()
        return state.fatigue

    def get_buffer_threshold(self, group_id: int) -> int:
        """
        根据参与度返回需要缓冲多少条消息才触发回复。
        曲线特征：
          100→70: 从1升到4（刚进聊天，几乎立即回复）
          70→40:  稳定在4（正常聊天节奏）
          40→0:   慢慢从4升到10（逐渐不想聊了）
        """
        eng = self.get_current_engagement(group_id)
        if eng <= 0:
            return 999999
        if eng >= 70:
            return max(1, int(1 + (100 - eng) / 10))
        elif eng >= 40:
            return 4
        else:
            return min(10, int(4 + (40 - eng) * 6 / 40))

    # ============================================================
    # 消息缓冲 
    # ============================================================

    async def buffer_message(self, event: GroupMessageEvent) -> Optional[List[BufferedMessage]]:
        """
        缓冲消息。
        @-mention → 立即返回（含缓冲区里已有的消息）。
        活跃期普通消息 → 加入缓冲区，达到阈值则返回全部。
        非活跃期 → 丢弃，返回 None。
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
            has_sticker=event.has_sticker,
        )

        # @-mention：立即返回（参与度由调用方设置）
        if event.is_at_bot:
            pending = state.buffer.copy()
            state.buffer.clear()
            pending.append(msg)
            return pending

        # 非活跃期直接丢弃
        if not self.is_active(gid):
            return None

        # 活跃期：加入缓冲区
        state.buffer.append(msg)

        # 达到阈值 → 取出全部交给 LLM
        threshold = self.get_buffer_threshold(gid)
        if len(state.buffer) >= threshold:
            messages = state.buffer.copy()
            state.buffer.clear()
            return messages

        # 保底回复：缓冲区有消息 + 距上次回复超过 40 秒 → 强制刷新
        if state.buffer and state.last_reply_time > 0:
            elapsed = time.time() - state.last_reply_time
            if elapsed >= GUARANTEED_REPLY_INTERVAL:
                messages = state.buffer.copy()
                state.buffer.clear()
                return messages

        return None

    # ============================================================
    # 随机出没调度
    # ============================================================

    def _is_night(self) -> bool:
        """是否为深夜时段（0:00-8:00）"""
        hour = datetime.now().hour
        return 0 <= hour < 8

    def _get_session_params(self) -> tuple:
        """根据当前时间返回出没参数：(gap_min, gap_max, engagement)"""
        if self._is_night():
            return NIGHT_GAP_MIN, NIGHT_GAP_MAX, NIGHT_ENGAGEMENT
        return DAY_GAP_MIN, DAY_GAP_MAX, DAY_ENGAGEMENT

    def _schedule_next_session(self, state: GroupState):
        """安排下一次随机出没时间"""
        gap_min, gap_max, _ = self._get_session_params()
        gap = random.randint(gap_min, gap_max)
        state.next_session_time = time.time() + gap * 60
        logger.debug("Next session in %d min", gap)

    def _check_random_session(self, group_id: int):
        """
        检查活跃状态（参与度 > 0 = 活跃）：
        1. 参与度 > 0 → 活跃中
        2. 参与度刚归零 → 清空缓冲、触发总结
        3. 到了出没时间 → 设置参与度
        """
        state = self._get_state(group_id)
        now = time.time()
        eng = self.get_current_engagement(group_id)

        # 活跃中
        if eng > 0:
            return True

        # 参与度刚归零（session 启动时 next_session_time 被清零，用此判断是否刚结束）
        if state.next_session_time == 0:
            # session 结束：清空状态，安排下次出没
            if state.reply_count > 0 or state.fatigue > 0:
                if self._on_session_end:
                    asyncio.ensure_future(self._on_session_end(group_id))
            state.buffer.clear()
            state.fatigue = 0
            state.fatigue_set_time = 0
            state.reply_count = 0
            state.at_count = 0
            state.at_charges = 2
            state.at_last_recharge = 0
            state.active_users.clear()
            state.last_reply_time = 0
            if state.session:
                state.session.clear()
            self._schedule_next_session(state)
            logger.info("[Group %d] Session ended", group_id)
            return False

        # 到了出没时间
        if state.next_session_time > 0 and now >= state.next_session_time:
            _, _, engage = self._get_session_params()
            state.engagement = engage
            state.engage_set_time = now
            state.fatigue = 0
            state.fatigue_set_time = now
            state.reply_count = 0
            state.at_count = 0
            state.at_charges = 2
            state.next_session_time = 0  # 清零，用于标记 session 进行中
            state.at_last_recharge = 0
            state.active_users.clear()
            state.session = ConversationSession()
            state.last_reply_time = now
            logger.info("[Group %d] Random session (engagement=%d)", group_id, engage)
            if self._on_session_start:
                asyncio.ensure_future(self._on_session_start(group_id))
            return True

        return False

    async def force_session(self, group_id: int):
        """强制进入随机出没状态，跳过冷却。"""
        state = self._get_state(group_id)
        now = time.time()
        _, _, engage = self._get_session_params()
        state.engagement = engage
        state.engage_set_time = now
        state.fatigue = 0
        state.fatigue_set_time = now
        state.reply_count = 0
        state.at_count = 0
        state.at_charges = 2
        state.next_session_time = 0
        state.at_last_recharge = 0
        state.active_users.clear()
        state.session = ConversationSession()
        state.last_reply_time = now
        logger.info("[Group %d] Forced session (engagement=%d)", group_id, engage)
        if self._on_session_start:
            await self._on_session_start(group_id)

    # ============================================================
    # 状态查询 + 后处理
    # ============================================================

    def get_session_status(self, group_id: int = 0) -> str:
        if group_id:
            eng = self.get_current_engagement(group_id)
            state = self._get_state(group_id)
            fat = state.fatigue
            night = " [深夜]" if self._is_night() else ""
            if eng > 0:
                buf_len = len(state.buffer)
                threshold = self.get_buffer_threshold(group_id)
                decay_left = int(ENGAGE_DECAY_DURATION * eng / 100)
                return f"ACTIVE (eng={eng:.0f}, fat={fat:.0f}, buf={buf_len}/{threshold}, ~{decay_left}s){night}"
            if state.next_session_time:
                gap = int(state.next_session_time - time.time())
                if gap > 0:
                    return f"IDLE (next in {gap // 60}m{gap % 60}s){night}"
            return f"IDLE{night}"
        lines = []
        for gid in self._groups:
            lines.append(f"  Group {gid}: {self.get_session_status(gid)}")
        return "\n".join(lines) if lines else "No groups active"

    def get_all_group_ids(self) -> List[int]:
        return list(self._groups.keys())

    def is_rejected(self, text: str) -> bool:
        """检查是否为 API 错误/风控信息"""
        if not text:
            print("[Filter] Rejected: empty response")
            return True
        lower = text.lower()
        for pattern in REJECT_PATTERNS:
            if re.search(pattern, lower):
                print(f"[Filter] Rejected: {text[:60]}")
                return True
        return False

    def post_process_reply(self, text: str) -> str:
        """回复后处理：去名字前缀、去AI痕迹、加语气词、限省略号、截断"""
        if not text:
            return text
        # 去掉 LLM 模仿聊天历史格式加的名字前缀 [xxx]
        text = re.sub(r"^\[[^\]]+\]\s*", "", text)
        for pattern in FORMAL_PATTERNS:
            text = re.sub(pattern, "", text)
        text = text.strip()
        # 限制省略号：只保留第一个，其余替换为句号
        if text.count("...") > 1:
            parts = text.split("...")
            text = parts[0] + "..." + "。".join(p for p in parts[1:] if p)
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
