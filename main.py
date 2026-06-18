import asyncio
import random
import re
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, Optional, List

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from config import settings
from core.event_handler import EventHandler, GroupMessageEvent, PrivateMessageEvent
from core.napcat import NapCatAPIClient
from core.onebot_manager import NapCatManager
from core.scheduler import BackgroundScheduler
from humanizer.humanizer import Humanizer, BufferedMessage
from llm.mimo import LLMClient, ConversationSession
from memory.database import Database
from memory.group_memory import GroupMemory
from memory.relationship import RelationshipGraph
from memory.group_file_memory import GroupFileMemory
from memory.user_file_memory import UserFileMemory
from memory.user_memory import UserMemory
from personality.persona import Personality
from utils.logger import setup_logger

logger = setup_logger("main")

# --- Initialize all components ---
api_client = NapCatAPIClient(settings.onebot_http_url, settings.onebot_access_token)
event_handler = EventHandler(settings)
onebot_manager = NapCatManager(settings)
humanizer = Humanizer(settings)
personality = Personality()
llm_client = LLMClient(settings)
db = Database(settings.db_path)
user_memory = UserMemory(db)
group_memory = GroupMemory(db)
relationship = RelationshipGraph(db)
user_file_memory = UserFileMemory(db, llm_client, settings)
group_file_memory = GroupFileMemory(db, llm_client, settings)
recent_messages: deque[GroupMessageEvent] = deque(maxlen=50)
scheduler = BackgroundScheduler(db, user_memory, group_memory, llm_client, settings)

loop: Optional[asyncio.AbstractEventLoop] = None


# --- Per-group message timestamps ---
_last_group_msg_time: Dict[int, float] = {}
_last_human_msg_time: Dict[int, float] = {}  # 仅记录人类消息时间
_last_bot_msg_time: Dict[int, float] = {}    # 仅记录 bot 消息时间

# --- 每群最近30条消息滚动容器（始终保存，不受活跃状态影响）---
_recent_chat: Dict[int, list] = {}  # group_id -> [{"role": ..., "content": ..., "images": [...]}, ...]
_RECENT_CHAT_LIMIT = 30


def _add_recent_chat(group_id: int, role: str, content: str, images: list = None):
    """往滚动容器里追加一条消息"""
    if group_id not in _recent_chat:
        _recent_chat[group_id] = []
    _recent_chat[group_id].append({"role": role, "content": content, "images": images or []})
    if len(_recent_chat[group_id]) > _RECENT_CHAT_LIMIT:
        _recent_chat[group_id] = _recent_chat[group_id][-_RECENT_CHAT_LIMIT:]


def _get_recent_chat(group_id: int) -> list:
    """获取滚动容器里的消息"""
    return _recent_chat.get(group_id, [])


def _update_recent_chat_last(group_id: int, new_content: str):
    """更新滚动容器里最后一条消息的内容"""
    if group_id in _recent_chat and _recent_chat[group_id]:
        _recent_chat[group_id][-1]["content"] = new_content


async def _enrich_recent_chat_images(group_id: int):
    """批量处理滚动容器中未描述的图片"""
    recent = _get_recent_chat(group_id)
    for msg in recent:
        if msg["role"] == "user" and msg.get("images") and "[图片]" in msg["content"] and "[图片:" not in msg["content"]:
            img_desc = await llm_client.describe_images(msg["images"])
            if img_desc and img_desc != "[图片]":
                msg["content"] = msg["content"].replace("[图片]", f"[图片: {img_desc}]", 1)
            msg["images"] = []  # 清除 URL，避免重复处理


# --- Session end callback ---
async def on_session_end(group_id: int):
    replied_user_ids = humanizer.get_replied_users(group_id)
    if not replied_user_ids:
        return
    print(f"\n[Memory] Session ended (group {group_id}), summarizing {len(replied_user_ids)} users...")
    for uid in replied_user_ids:
        row = await db.fetchone("SELECT nickname FROM users WHERE qq_id = ?", (uid,))
        if row:
            await user_file_memory.summarize_and_save(uid, row[0])
        else:
            print(f"[Memory] user {uid}: not found in DB, skip")
    await group_file_memory.summarize_and_save(group_id)
    print("[Memory] Done.\n")


# --- Session check loop (定期检查是否该出没) ---
async def session_check_loop():
    while True:
        try:
            await asyncio.sleep(60)
            for gid in list(settings.group_ids):
                humanizer._check_random_session(gid)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Session check error: %s", e)


async def _process_private_buffer(user_id: int, buffered: list):
    """处理私聊缓冲区消息"""
    state = _get_private_state(user_id)

    # 存 DB
    for msg in buffered:
        await group_memory.save_message(0, msg["user_id"], msg["nickname"], msg["text"])

    # 构建上下文
    user_ctx = await build_user_context(user_id)
    session_msgs = state.session.get_messages() if state.session else []
    if not session_msgs:
        raw_msgs = await group_memory.get_recent_messages(0, limit=50)
        for msg in raw_msgs:
            if msg["role"] == "assistant":
                session_msgs.append({"role": "assistant", "content": f'[小夜] {msg["content"]}'})
            else:
                session_msgs.append({"role": "user", "content": f'[{msg["nickname"]}] {msg["content"]}'})

    sys_prompt = personality.get_private_system_prompt(user_ctx)

    if state.reply_count > 10:
        sys_prompt += "\n\n你已经聊了很久了，开始有点累了。回复变短变敷衍。"
    elif state.reply_count > 5:
        sys_prompt += "\n\n你聊了一会儿了，回复可以简短一些。"

    user_message = "\n".join([f'{m["nickname"]}: {m["text"]}' for m in buffered])

    reply = await llm_client.generate_reply(sys_prompt, session_msgs, user_message)
    if not reply:
        return
    if personality.check_forbidden(reply):
        return

    reply = humanizer.post_process_reply(reply)
    if humanizer.is_rejected(reply):
        return

    await typing_delay(reply)
    await send_private_split(user_id, reply)
    state.reply_count += 1
    state.last_reply_time = time.time()

    if state.session:
        state.session.add_message("assistant", f'[小夜] {reply}')

    await group_memory.save_message(0, settings.bot_qq_id, personality.name, reply, is_bot=True)
    await relationship.update_closeness(user_id, 0.02)


async def guaranteed_reply_loop():
    """每10秒检查保底回复：缓冲区有消息 + 超过阈值未回复 → 强制处理"""
    while True:
        try:
            await asyncio.sleep(10)
            # 群聊保底
            flushed = humanizer.check_guaranteed_reply()
            for gid, pending in flushed:
                logger.info("[Group %d] Guaranteed reply: %d buffered messages", gid, len(pending))
                # 低耗模式：补全图片描述
                if not settings.high_resource_mode:
                    for m in pending:
                        if m.images and "[图片]" in m.text and "[图片:" not in m.text:
                            img_desc = await llm_client.describe_images(m.images)
                            if img_desc and img_desc != "[图片]":
                                m.text = m.text.replace("[图片]", f"[图片: {img_desc}]", 1)
                await process_buffered_messages(gid, pending)
            # 私聊保底
            for uid, state in list(_private_states.items()):
                if state.buffer and state.last_reply_time > 0:
                    elapsed = time.time() - state.last_reply_time
                    if elapsed >= 30:
                        buffered = state.buffer.copy()
                        state.buffer.clear()
                        logger.info("[Private %d] Guaranteed reply: %d buffered messages", uid, len(buffered))
                        await _process_private_buffer(uid, buffered)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Guaranteed reply error: %s", e)


# --- Terminal Dashboard ---
_debug_mode = False
_dashboard_lines = 0  # how many lines the dashboard occupies


def _render_dashboard():
    """Render a real-time status dashboard to terminal."""
    global _dashboard_lines
    if not _debug_mode:
        return

    lines = []
    lines.append("═" * 60)
    lines.append("  CyberHomie Debug Dashboard")
    lines.append("═" * 60)

    for gid in settings.group_ids:
        state = humanizer._get_state(gid)
        eng = humanizer.get_current_engagement(gid)
        fat = state.fatigue

        if humanizer.is_active(gid):
            buf_count = len(state.buffer)
            threshold = humanizer.get_buffer_threshold(gid)
            decay_left = int(300 * eng / 100)
            replied = len(state.active_users)
            status = f"ACTIVE (~{decay_left}s)"
            lines.append(f"  Group {gid}")
            lines.append(f"    Status:  {status}")
            lines.append(f"    Engage:  {'█' * int(eng / 5)}{'░' * (20 - int(eng / 5))} {eng:.0f}/100")
            lines.append(f"    Fatigue: {'█' * int(fat / 5)}{'░' * (20 - int(fat / 5))} {fat:.0f}/100")
            lines.append(f"    Buffer:  {buf_count}/{threshold} msgs")
            lines.append(f"    Replied: {replied} users")
        elif state.next_session_time:
            gap = int(state.next_session_time - time.time())
            lines.append(f"  Group {gid}")
            lines.append(f"    Status:  IDLE (next in {gap // 60}m{gap % 60}s)")
        else:
            lines.append(f"  Group {gid}")
            lines.append(f"    Status:  IDLE")

    # Private chat
    if _private_states:
        lines.append("  Private Chats:")
        for uid, state in sorted(_private_states.items(), key=lambda x: -x[1].reply_count):
            if state.reply_count > 0:
                lines.append(f"    {uid}: {state.reply_count} replies, buf={len(state.buffer)}")

    lines.append("═" * 60)
    lines.append("  Type 'debug' to toggle | 'help' for commands")
    lines.append("═" * 60)

    # Move cursor up to overwrite previous dashboard
    if _dashboard_lines > 0:
        print(f"\033[{_dashboard_lines}A", end="")

    # Clear and print
    for line in lines:
        print(f"\033[2K{line}")

    _dashboard_lines = len(lines)


async def dashboard_loop():
    """Refresh dashboard every 2 seconds."""
    while True:
        try:
            await asyncio.sleep(2)
            if _debug_mode:
                _render_dashboard()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# --- 私聊状态（类似群聊的缓冲区机制）---
@dataclass
class PrivateChatState:
    engagement: float = 0.0
    engage_set_time: float = 0.0
    buffer: list = field(default_factory=list)
    session: Optional[ConversationSession] = None
    last_reply_time: float = 0.0
    reply_count: int = 0

_private_states: Dict[int, PrivateChatState] = {}


def _get_private_state(user_id: int) -> PrivateChatState:
    if user_id not in _private_states:
        _private_states[user_id] = PrivateChatState()
    return _private_states[user_id]


def _is_private_active(user_id: int) -> bool:
    state = _get_private_state(user_id)
    if state.engagement <= 0:
        return False
    elapsed = time.time() - state.engage_set_time
    decay = elapsed / _PRIVATE_ENGAGEMENT_DECAY * 100
    return max(0.0, state.engagement - decay) > 0


async def on_session_start(group_id: int):
    """随机出没启动时，根据最近聊天内容接话。"""
    group_file = build_group_context(group_id)
    group_ctx_list = await group_memory.get_important_memories(group_id)
    if group_file:
        group_ctx_list.append(group_file)
    sys_prompt = personality.get_system_prompt("", "\n".join(group_ctx_list))

    # 从滚动容器取最近消息，预填充到 session 窗口
    state = humanizer._get_state(group_id)
    recent = _get_recent_chat(group_id)
    if not recent:
        logger.info("[Group %d] Session start skipped: no recent messages", group_id)
        return

    # 低耗模式：session 启动时补全历史图片描述
    if not settings.high_resource_mode:
        await _enrich_recent_chat_images(group_id)

    # 预填充 session 窗口
    session_msgs = state.session.get_messages() if state.session else []
    if not session_msgs:
        for msg in recent:
            state.session.add_message(msg["role"], msg["content"])
        session_msgs = recent

    # 如果最后一条是 bot 发的，跳过（防止自言自语）
    if session_msgs and session_msgs[-1].get("role") == "assistant":
        logger.info("[Group %d] Session start skipped: last msg is bot's", group_id)
        return

    # 根据最近聊天内容接话
    text = await llm_client.generate_join_reply(sys_prompt, session_msgs)
    if not text:
        print(f"[Bot][群{group_id}] join_reply: LLM returned empty")
        return
    if personality.check_forbidden(text):
        print(f"[Bot][群{group_id}] join_reply: forbidden pattern -> {text[:40]}")
        return

    text = humanizer.post_process_reply(text)
    if humanizer.is_rejected(text):
        print(f"[Bot][群{group_id}] join_reply: rejected -> {text[:40]}")
        return

    await typing_delay(text)
    await api_client.send_group_message(group_id, text)
    _last_bot_msg_time[group_id] = time.time()
    _add_recent_chat(group_id, "assistant", f'[小夜] {text}')
    humanizer.notify_bot_replied(group_id)
    logger.info("[Bot][群%d][接话] -> %s", group_id, text[:60])
    if state.session:
        state.session.add_message("assistant", f'[小夜] {text}')
    await group_memory.save_message(
        group_id, settings.bot_qq_id, personality.name, text, is_bot=True
    )


# --- Typing delay ---
async def typing_delay(text: str):
    """模拟打字延迟"""
    per_char = settings.typing_delay_per_char
    base = len(text) * per_char
    delay = min(10.0, max(3.0, base)) + random.uniform(0, 1.0)
    await asyncio.sleep(delay)


def split_message(text: str) -> list[str]:
    """拆分消息：按换行和省略号拆分，模拟真人分条发消息"""
    parts = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 按省略号拆分
        segments = re.split(r"\.{3,}|…", line)
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            if len(seg) > 150:
                for i in range(0, len(seg), 150):
                    parts.append(seg[i:i+150])
            else:
                parts.append(seg)
    return parts if parts else [text]


async def send_group_split(group_id: int, text: str, reply_to: int = 0):
    """分条发送群消息，第一条可引用回复"""
    parts = split_message(text)
    for i, part in enumerate(parts):
        if i > 0:
            await typing_delay(part)
        rt = reply_to if i == 0 else 0
        await api_client.send_group_message(group_id, part, reply_to=rt)
        logger.info("[Bot][群%d] -> %s", group_id, part[:60])


async def send_private_split(user_id: int, text: str):
    """分条发送私聊消息"""
    parts = split_message(text)
    for i, part in enumerate(parts):
        if i > 0:
            await typing_delay(part)
        await api_client.send_private_message(user_id, part)
        logger.info("[Bot][私聊] -> %s", part[:60])


# --- Build context for LLM ---
async def build_user_context(user_id: int) -> str:
    user_ctx = await user_memory.get_user_summary(user_id)
    file_memory = user_file_memory.load_for_prompt(user_id)
    if file_memory:
        user_ctx += f"\n长期记忆:\n{file_memory}"
    bot_rel = await relationship.get_bot_relationship(user_id)
    if bot_rel:
        user_ctx += f"\n我和ta的关系: {bot_rel}"
    return user_ctx


async def build_chat_history(group_id: int, limit: int = 50) -> list[dict]:
    raw_msgs = await group_memory.get_recent_messages(group_id, limit=limit)
    history = []
    for msg in raw_msgs:
        if msg["role"] == "assistant":
            # bot 的消息带名字，让 LLM 知道是自己说的
            history.append({"role": "assistant", "content": f'[小夜] {msg["content"]}'})
        else:
            # 其他人的消息带名字
            history.append({"role": "user", "content": f'[{msg["nickname"]}] {msg["content"]}'})
    return history


def build_group_context(group_id: int) -> str:
    """Load group memory file (sorted by importance, truncated)."""
    return group_file_memory.load_for_prompt(group_id)


# --- App lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    loop = asyncio.get_event_loop()
    await db.initialize()

    humanizer.set_session_start_callback(on_session_start)
    humanizer.set_session_end_callback(on_session_end)

    scheduler.start()
    logger.info("CyberHomie started")

    # 启动 OneBot 后端
    if await onebot_manager.ensure_installed():
        onebot_manager.generate_config()
        await onebot_manager.start()
    else:
        logger.warning("OneBot not installed, run 'python setup.py' first")

    import threading
    terminal_thread = threading.Thread(target=terminal_loop, daemon=True)
    terminal_thread.start()
    session_task = asyncio.create_task(session_check_loop())
    guarantee_task = asyncio.create_task(guaranteed_reply_loop())
    dash_task = asyncio.create_task(dashboard_loop())
    print("\nType 'help' for commands.\n")

    yield

    session_task.cancel()
    guarantee_task.cancel()
    dash_task.cancel()

    await onebot_manager.stop()
    scheduler.shutdown()
    await llm_client.close()
    await api_client.close()
    await db.close()
    logger.info("CyberHomie stopped")


app = FastAPI(title="CyberHomie", lifespan=lifespan)


async def process_buffered_messages(group_id: int, pending: list, trigger_user_id: int = 0):
    """处理缓冲区消息：存DB → 构建上下文 → LLM决策 → 发送回复"""
    state = humanizer._get_state(group_id)
    was_at = any(m.is_at_bot for m in pending)

    for msg in pending:
        await group_memory.save_message(group_id, msg.user_id, msg.nickname, msg.text)
        await user_memory.get_or_create_user(msg.user_id, msg.nickname)
        await user_memory.increment_message_count(msg.user_id)

    # 用触发用户或最后一条消息的用户做上下文
    uid = trigger_user_id or (pending[-1].user_id if pending else 0)
    user_ctx = await build_user_context(uid) if uid else ""
    group_ctx_list = await group_memory.get_important_memories(group_id)
    group_file = build_group_context(group_id)
    if group_file:
        group_ctx_list.append(group_file)

    session_msgs = state.session.get_messages() if state.session else []
    if not session_msgs:
        session_msgs = _get_recent_chat(group_id) or await build_chat_history(group_id)

    sys_prompt = personality.get_system_prompt(user_ctx, "\n".join(group_ctx_list))

    buffered_data = [
        {
            "message_id": m.message_id,
            "nickname": m.nickname,
            "text": m.text,
            "is_at_bot": m.is_at_bot,
        }
        for m in pending
    ]

    replies = await llm_client.decide_replies(sys_prompt, session_msgs, buffered_data)

    if not replies:
        print(f"[LLM] Group {group_id}: No reply needed")

    for reply in replies:
        text = reply.get("text", "")
        if not text:
            continue
        msg_id = reply.get("message_id", 0)
        quote = reply.get("quote", False)

        if personality.check_forbidden(text):
            continue

        text = humanizer.post_process_reply(text)
        if humanizer.is_rejected(text):
            continue
        reply_to = msg_id if quote else 0

        await typing_delay(text)
        await send_group_split(group_id, text, reply_to=reply_to)
        _last_bot_msg_time[group_id] = time.time()
        _add_recent_chat(group_id, "assistant", f'[小夜] {text}')
        humanizer.notify_bot_replied(group_id, was_at=was_at)

        if state.session:
            state.session.add_message("assistant", f'[小夜] {text}')

        await group_memory.save_message(
            group_id, settings.bot_qq_id, personality.name, text, is_bot=True
        )
        if uid:
            await relationship.update_closeness(uid, 0.01)
            humanizer.record_replied_user(group_id, uid)


# --- Group message handler ---
async def handle_group_message(event: GroupMessageEvent):
    _last_group_msg_time[event.group_id] = time.time()
    _last_human_msg_time[event.group_id] = time.time()

    # 高耗模式：立即解析图片；低耗模式：只存标签，延迟到活跃期处理
    msg_text = event.raw_text
    if event.has_sticker:
        msg_text = f"{msg_text} [表情包]" if msg_text else "[表情包]"
    if event.images and not event.has_sticker:
        if settings.high_resource_mode:
            print(f"[群{event.group_id}] 图片入: {event.images}")
            img_desc = await llm_client.describe_images(event.images)
            if img_desc and img_desc != "[图片]":
                msg_text = f"{msg_text} [图片: {img_desc}]" if msg_text else f"[图片: {img_desc}]"
            else:
                msg_text = f"{msg_text} [图片]" if msg_text else "[图片]"
            # 同步更新 event.raw_text，让缓冲区也用新文本
            event.raw_text = msg_text
        else:
            msg_text = f"{msg_text} [图片]" if msg_text else "[图片]"

    # 存入滚动容器（保留图片 URL 供低耗模式后续补描述）
    _add_recent_chat(event.group_id, "user", f'[{event.nickname}] {msg_text}', images=event.images)
    logger.info("[群%d][%s] %s (at=%s)", event.group_id, event.nickname, msg_text[:60], event.is_at_bot)
    recent_messages.append(event)

    # 录入 session 滚动窗口
    state = humanizer._get_state(event.group_id)
    if state.session and humanizer.is_active(event.group_id):
        state.session.add_message("user", f'[{event.nickname}] {msg_text}')

    pending = await humanizer.buffer_message(event)

    # @-mention：先检查是否应该回复，再设置参与度
    if event.is_at_bot:
        if not humanizer.should_reply_to_at(event.group_id):
            logger.info("[群%d] @ ignored (fatigue or no charges)", event.group_id)
            return
        # 沉默期被@：激活参与度 + 创建 session；活跃期被@：不重置
        if not humanizer.is_active(event.group_id):
            humanizer.trigger_active(event.group_id, 100, force=True)
            if not state.session:
                state.session = ConversationSession()
                state.last_reply_time = time.time()
                # 低耗模式：session 启动时补全历史图片描述
                if not settings.high_resource_mode:
                    await _enrich_recent_chat_images(event.group_id)
                    # 同步更新 pending 消息的文本（可能包含未描述的图片）
                    for m in pending:
                        if m.images and "[图片]" in m.text and "[图片:" not in m.text:
                            recent = _get_recent_chat(event.group_id)
                            for rmsg in recent:
                                if rmsg["content"].startswith(f'[{event.nickname}]'):
                                    m.text = rmsg["content"].replace(f'[{event.nickname}] ', '', 1)
                                    break
                # 预填充滚动容器历史到 session
                recent = _get_recent_chat(event.group_id)
                for msg in recent:
                    state.session.add_message(msg["role"], msg["content"])

    if pending is None:
        return

    # 低耗模式活跃期：处理当前消息的图片
    if not settings.high_resource_mode and event.images and not event.has_sticker:
        print(f"[群{event.group_id}] 图片入: {event.images}")
        img_desc = await llm_client.describe_images(event.images)
        if img_desc and img_desc != "[图片]":
            enriched = f'[{event.nickname}] {event.raw_text} [图片: {img_desc}]'
        else:
            enriched = f'[{event.nickname}] {event.raw_text} [图片]'
        _update_recent_chat_last(event.group_id, enriched)
        if state.session:
            state.session.update_last_user_message(enriched)
        for m in pending:
            if m.message_id == event.message_id:
                m.text = f'{event.raw_text} [图片: {img_desc}]' if img_desc and img_desc != "[图片]" else f'{event.raw_text} [图片]'
                break

    await process_buffered_messages(event.group_id, pending, trigger_user_id=event.user_id)


# --- Private message handler (带缓冲区机制) ---
async def handle_private_message(event: PrivateMessageEvent):
    logger.info("[私聊][%s] %s", event.nickname, event.raw_text[:80])

    await user_memory.get_or_create_user(event.user_id, event.nickname)
    await user_memory.increment_message_count(event.user_id)

    # 提前处理图片（遵循资源模式设置）
    msg_text = event.raw_text
    if event.images and not event.has_sticker:
        if settings.high_resource_mode:
            img_desc = await llm_client.describe_images(event.images)
            if img_desc and img_desc != "[图片]":
                msg_text = f"{msg_text} [图片: {img_desc}]" if msg_text else f"[图片: {img_desc}]"
            elif event.images:
                msg_text = f"{msg_text} [图片]" if msg_text else "[图片]"
        else:
            msg_text = f"{msg_text} [图片]" if msg_text else "[图片]"
    if event.has_sticker:
        msg_text = f"{msg_text} [表情包]" if msg_text else "[表情包]"

    # 激活私聊参与度
    state = _get_private_state(event.user_id)
    state.engagement = 100
    state.engage_set_time = time.time()

    # 创建 session（如果没有）
    if not state.session:
        state.session = ConversationSession()

    # 录入 session
    state.session.add_message("user", f'[{event.nickname}] {msg_text}')

    # 加入缓冲区
    state.buffer.append({"user_id": event.user_id, "nickname": event.nickname, "text": msg_text})

    # 检查是否应该回复
    should_reply = False

    # 缓冲区达到阈值（2条消息）
    if len(state.buffer) >= 2:
        should_reply = True

    # 保底回复：距上次回复超过 30 秒
    if state.last_reply_time > 0 and time.time() - state.last_reply_time >= 30:
        should_reply = True

    # 首条消息直接回复
    if state.last_reply_time == 0:
        should_reply = True

    if not should_reply:
        return

    # 处理缓冲区
    buffered = state.buffer.copy()
    state.buffer.clear()
    await _process_private_buffer(event.user_id, buffered)


@app.websocket("/onebot/ws")
async def onebot_ws(websocket: WebSocket):
    await websocket.accept()
    logger.info("NapCat WebSocket connected")

    try:
        while True:
            data = await websocket.receive_json()

            # 调试：记录收到的事件类型
            post_type = data.get("post_type", "")
            msg_type = data.get("message_type", "")
            logger.debug("WS event: post_type=%s message_type=%s", post_type, msg_type)

            group_event = event_handler.parse_group_message(data)
            if group_event:
                await handle_group_message(group_event)
                continue

            private_event = event_handler.parse_private_message(data)
            if private_event:
                await handle_private_message(private_event)
                continue

    except WebSocketDisconnect:
        logger.warning("NapCat WebSocket disconnected")
    except Exception as e:
        logger.error("WebSocket error: %s", e, exc_info=True)


# --- Terminal commands ---
def terminal_loop():
    """Blocking stdin reader in a thread. Puts commands into asyncio queue."""
    import threading
    while True:
        try:
            cmd = input()
            if cmd.strip():
                asyncio.run_coroutine_threadsafe(handle_command(cmd.strip()), loop)
        except EOFError:
            break
        except Exception as e:
            logger.error("Terminal error: %s", e)


async def handle_command(cmd: str):
    parts = cmd.split()
    if not parts:
        return
    action = parts[0].lower()

    if action == "help":
        print("""
  status                      查看所有群状态
  engage <群号> [0-100]        查看/设置参与度
  wake <群号>                 跳过冷却，立即随机出没
  say [群号] <消息>            以bot身份发群消息
  summarize                   总结所有用户+群记忆
  summarize <qq_id>           总结指定用户
  summarize group             总结群记忆
  debug                       切换实时状态面板
  help                        显示帮助
""")

    elif action == "status":
        print(f"\nGroups: {settings.group_ids}")
        print(humanizer.get_session_status())
        print()

    elif action == "summarize":
        if len(parts) >= 2 and parts[1] == "group":
            for gid in settings.group_ids:
                print(f"[Memory] Summarizing group {gid}...")
                await group_file_memory.summarize_and_save(gid)
            print("[Memory] Done.")
        elif len(parts) >= 2:
            qq_id = int(parts[1])
            row = await db.fetchone("SELECT nickname FROM users WHERE qq_id = ?", (qq_id,))
            if row:
                print(f"[Memory] Summarizing {row[0]} ({qq_id})...")
                await user_file_memory.summarize_and_save(qq_id, row[0])
                print(f"[Memory] Done. Saved to data/memory/{qq_id}.md")
            else:
                print(f"User {qq_id} not found")
        else:
            print("[Memory] Summarizing all...")
            rows = await db.fetchall(
                "SELECT qq_id, nickname FROM users WHERE message_count >= 1 "
                "ORDER BY last_seen DESC LIMIT 20"
            )
            for qq_id, nickname in rows:
                print(f"[Memory] {nickname} ({qq_id})...")
                await user_file_memory.summarize_and_save(qq_id, nickname)
            for gid in settings.group_ids:
                print(f"[Memory] Summarizing group {gid}...")
                await group_file_memory.summarize_and_save(gid)
            print(f"[Memory] Done. {len(rows)} users + {len(settings.group_ids)} groups.")

    elif action == "say":
        if len(parts) >= 2:
            # say [group_id] <message>
            gid = settings.target_group_ids.split(",")[0].strip()
            msg_start = 1
            if len(parts) >= 3 and parts[1].isdigit():
                gid = parts[1]
                msg_start = 2
            msg = " ".join(parts[msg_start:])
            await api_client.send_group_message(int(gid), msg)
            _last_bot_msg_time[int(gid)] = time.time()
            _add_recent_chat(int(gid), "assistant", f'[小夜] {msg}')
            await group_memory.save_message(
                int(gid), settings.bot_qq_id, personality.name, msg, is_bot=True
            )
            print(f"[Bot][群{gid}] Sent: {msg}")
        else:
            print("Usage: say [群号] <消息>")

    elif action == "engage":
        if len(parts) >= 3:
            gid, level = int(parts[1]), float(parts[2])
            humanizer.trigger_active(gid, engagement=level, force=True)
            print(f"Group {gid} engagement set to {level}")
        elif len(parts) >= 2:
            gid = int(parts[1])
            print(f"Group {gid} engagement: {humanizer.get_current_engagement(gid):.0f}")
        else:
            for gid in settings.group_ids:
                print(f"  Group {gid}: {humanizer.get_current_engagement(gid):.0f}")

    elif action == "wake":
        if len(parts) >= 2:
            gid = int(parts[1])
            if gid in settings.group_ids:
                await humanizer.force_session(gid)
                print(f"[Wake] Group {gid} -> forced session")
            else:
                print(f"Group {gid} not in configured groups: {settings.group_ids}")
        else:
            print("Usage: wake <群号>")

    elif action == "debug":
        global _debug_mode
        _debug_mode = not _debug_mode
        if _debug_mode:
            print("\n[Debug] Dashboard ON (refreshes every 2s)")
            _render_dashboard()
        else:
            print("\n[Debug] Dashboard OFF")

    else:
        print(f"Unknown: {action}. Type 'help'.")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765)
