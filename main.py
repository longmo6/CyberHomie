import asyncio
import random
import re
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Dict, Optional, List

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from config import settings
from core.event_handler import EventHandler, GroupMessageEvent, PrivateMessageEvent
from core.napcat import NapCatAPIClient
from core.scheduler import BackgroundScheduler
from humanizer.humanizer import Humanizer, BufferedMessage
from llm.mimo import LLMClient
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
api_client = NapCatAPIClient(settings.napcat_http_url, settings.napcat_access_token)
event_handler = EventHandler(settings)
humanizer = Humanizer(settings)
personality = Personality()
llm_client = LLMClient(settings)
db = Database(settings.db_path)
user_memory = UserMemory(db)
group_memory = GroupMemory(db)
relationship = RelationshipGraph(db)
user_file_memory = UserFileMemory(db, llm_client)
group_file_memory = GroupFileMemory(db, llm_client)
recent_messages: deque[GroupMessageEvent] = deque(maxlen=50)
scheduler = BackgroundScheduler(db, user_memory, group_memory, llm_client, settings)

napcat_process: Optional[subprocess.Popen] = None
loop: Optional[asyncio.AbstractEventLoop] = None


# --- Per-group message timestamps ---
_last_group_msg_time: Dict[int, float] = {}
_last_human_msg_time: Dict[int, float] = {}  # 仅记录人类消息时间


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
    if _private_reply_count:
        lines.append("  Private Chats:")
        for uid, count in sorted(_private_reply_count.items(), key=lambda x: -x[1]):
            if count > 0:
                lines.append(f"    {uid}: {count} replies")

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


# --- 私聊参与度 ---
_private_reply_count: Dict[int, int] = {}  # user_id -> 连续回复次数
_private_last_reply: Dict[int, float] = {}  # user_id -> 上次回复时间
_PRIVATE_MAX_REPLIES = 15  # 连续回复超过此数停止回复
_PRIVATE_COOLDOWN = 1800   # 停止后冷却30分钟


async def on_session_start(group_id: int):
    """随机出没启动时，根据最后一条人类消息决定是插话还是开话题。"""
    group_file = build_group_context(group_id)
    group_ctx_list = await group_memory.get_important_memories(group_id)
    if group_file:
        group_ctx_list.append(group_file)
    sys_prompt = personality.get_system_prompt("", "\n".join(group_ctx_list))

    # session 窗口（刚创建时为空，回退到 DB 历史）
    state = humanizer._get_state(group_id)
    session_msgs = state.session.get_messages() if state.session else []
    if not session_msgs:
        session_msgs = await build_chat_history(group_id, limit=30)

    # 检查最近 2 分钟是否有人类活动
    last_msg_time = _last_human_msg_time.get(group_id, 0)
    recent_active = last_msg_time > 0 and time.time() - last_msg_time < 120

    if recent_active:
        # 群里正在聊天，根据历史记录插一句话
        text = await llm_client.generate_join_reply(sys_prompt, session_msgs)
        if not text:
            print(f"[Bot][群{group_id}] join_reply: LLM returned empty")
        elif personality.check_forbidden(text):
            print(f"[Bot][群{group_id}] join_reply: forbidden pattern -> {text[:40]}")
        else:
            text = humanizer.post_process_reply(text)
            if humanizer.is_rejected(text):
                print(f"[Bot][群{group_id}] join_reply: rejected -> {text[:40]}")
            else:
                await typing_delay(text)
                await api_client.send_group_message(group_id, text)
                humanizer.notify_bot_replied(group_id)
                logger.info("[Bot][群%d][插话] -> %s", group_id, text[:60])
                if state.session:
                    state.session.add_message("assistant", f'[小夜] {text}')
                await group_memory.save_message(
                    group_id, settings.bot_qq_id, personality.name, text, is_bot=True
                )
        return

    # 安静超过 2 分钟，开话题
    topic = await llm_client.generate_topic(sys_prompt, session_msgs)
    if not topic:
        print(f"[Bot][群{group_id}] generate_topic: LLM returned empty")
        return
    if personality.check_forbidden(topic):
        print(f"[Bot][群{group_id}] generate_topic: forbidden pattern -> {topic[:40]}")
        return

    topic = humanizer.post_process_reply(topic)
    if humanizer.is_rejected(topic):
        print(f"[Bot][群{group_id}] generate_topic: rejected -> {topic[:40]}")
        return
    await typing_delay(topic)
    await api_client.send_group_message(group_id, topic)
    humanizer.notify_bot_replied(group_id)
    logger.info("[Bot][群%d][主动] -> %s", group_id, topic[:60])
    if state.session:
        state.session.add_message("assistant", f'[小夜] {topic}')
    await group_memory.save_message(
        group_id, settings.bot_qq_id, personality.name, topic, is_bot=True
    )


# --- Typing delay ---
async def typing_delay(text: str):
    """模拟打字延迟：每字0.3秒，范围3-10秒，随机0-1秒"""
    base = len(text) * 0.3
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
    global napcat_process, loop
    loop = asyncio.get_event_loop()
    await db.initialize()

    humanizer.set_session_start_callback(on_session_start)
    humanizer.set_session_end_callback(on_session_end)

    scheduler.start()
    logger.info("CyberHomie started")

    if settings.napcat_path:
        import os
        napcat_dir = os.path.dirname(settings.napcat_path)
        napcat_process = subprocess.Popen(
            settings.napcat_path,
            cwd=napcat_dir if napcat_dir else None,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        logger.info("NapCat launched (pid=%d)", napcat_process.pid)
    else:
        logger.info("NAPCAT_PATH not set, start NapCat manually")

    import threading
    terminal_thread = threading.Thread(target=terminal_loop, daemon=True)
    terminal_thread.start()
    session_task = asyncio.create_task(session_check_loop())
    dash_task = asyncio.create_task(dashboard_loop())
    print("\nType 'help' for commands.\n")

    yield

    session_task.cancel()
    dash_task.cancel()

    if napcat_process and napcat_process.poll() is None:
        napcat_process.terminate()
        logger.info("NapCat terminated")
    scheduler.shutdown()
    await llm_client.close()
    await api_client.close()
    await db.close()
    logger.info("CyberHomie stopped")


app = FastAPI(title="CyberHomie", lifespan=lifespan)


# --- Group message handler ---
async def handle_group_message(event: GroupMessageEvent):
    _last_group_msg_time[event.group_id] = time.time()
    _last_human_msg_time[event.group_id] = time.time()
    logger.info("[群%d][%s] %s (at=%s)", event.group_id, event.nickname, event.raw_text[:60], event.is_at_bot)
    recent_messages.append(event)

    # 录入 session 滚动窗口
    state = humanizer._get_state(event.group_id)
    if state.session and humanizer.is_active(event.group_id):
        state.session.add_message("user", f'[{event.nickname}] {event.raw_text}')

    pending = await humanizer.buffer_message(event)
    if pending is None:
        return

    # @-mention：先检查是否应该回复，再设置参与度
    if event.is_at_bot:
        if not humanizer.should_reply_to_at(event.group_id):
            logger.info("[群%d] @ ignored (fatigue or no charges)", event.group_id)
            return
        # 沉默期被@：激活参与度；活跃期被@：不重置
        if not humanizer.is_active(event.group_id):
            humanizer.trigger_active(event.group_id, 100, force=True)

    for msg in pending:
        await group_memory.save_message(
            event.group_id, msg.user_id, msg.nickname, msg.text
        )
        await user_memory.get_or_create_user(msg.user_id, msg.nickname)
        await user_memory.increment_message_count(msg.user_id)

    user_ctx = await build_user_context(event.user_id)
    group_ctx_list = await group_memory.get_important_memories(event.group_id)
    group_file = build_group_context(event.group_id)
    if group_file:
        group_ctx_list.append(group_file)

    # 优先用 session 窗口，否则从 DB 取
    session_msgs = state.session.get_messages() if state.session else []
    if not session_msgs:
        session_msgs = await build_chat_history(event.group_id)

    sys_prompt = personality.get_system_prompt(user_ctx, "\n".join(group_ctx_list))

    # For @-mention, use single-message decision
    buffered_data = [
        {
            "message_id": m.message_id,
            "nickname": m.nickname,
            "text": m.text,
            "is_at_bot": m.is_at_bot,
            "images": m.images,
            "has_sticker": m.has_sticker,
        }
        for m in pending
    ]

    print(f"[LLM] Processing {len(pending)} messages (@-mention)")
    replies = await llm_client.decide_replies(sys_prompt, session_msgs, buffered_data)

    if not replies:
        print("[LLM] No reply needed")

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
        await send_group_split(event.group_id, text, reply_to=reply_to)
        humanizer.notify_bot_replied(event.group_id, was_at=True)

        # 录入 session
        if state.session:
            state.session.add_message("assistant", f'[小夜] {text}')

        await group_memory.save_message(
            event.group_id, settings.bot_qq_id, personality.name, text, is_bot=True
        )
        await relationship.update_closeness(event.user_id, 0.01)
        humanizer.record_replied_user(event.group_id, event.user_id)


# --- Private message handler (unchanged, immediate reply) ---
async def handle_private_message(event: PrivateMessageEvent):
    logger.info("[私聊][%s] %s", event.nickname, event.raw_text[:80])

    # 私聊参与度检查
    count = _private_reply_count.get(event.user_id, 0)
    if count >= _PRIVATE_MAX_REPLIES:
        # 冷却中，检查是否过了冷却期
        last_reply = _private_last_reply.get(event.user_id, 0)
        if time.time() - last_reply > _PRIVATE_COOLDOWN:
            _private_reply_count[event.user_id] = 0  # 重置
        else:
            return  # 还在冷却中

    await user_memory.get_or_create_user(event.user_id, event.nickname)
    await user_memory.increment_message_count(event.user_id)
    await group_memory.save_message(0, event.user_id, event.nickname, event.raw_text)

    user_ctx = await build_user_context(event.user_id)
    raw_msgs = await group_memory.get_recent_messages(0, limit=50)
    chat_history = []
    for msg in raw_msgs:
        if msg["role"] == "assistant":
            chat_history.append({"role": "assistant", "content": f'[小夜] {msg["content"]}'})
        else:
            chat_history.append({"role": "user", "content": f'[{msg["nickname"]}] {msg["content"]}'})

    sys_prompt = personality.get_private_system_prompt(user_ctx)

    # 根据连续回复次数调整行为
    if count > 10:
        sys_prompt += "\n\n你已经聊了很久了，开始有点累了。回复变短变敷衍，可以只回一个字或表情。"
    elif count > 5:
        sys_prompt += "\n\n你聊了一会儿了，开始有点想做别的事了。回复可以简短一些。"

    reply = await llm_client.generate_reply(
        sys_prompt, chat_history, event.raw_text,
        images=event.images if event.images else None,
    )
    if not reply:
        return
    if personality.check_forbidden(reply):
        return

    reply = humanizer.post_process_reply(reply)
    if humanizer.is_rejected(reply):
        return
    await typing_delay(reply)
    await send_private_split(event.user_id, reply)
    _private_reply_count[event.user_id] = count + 1
    _private_last_reply[event.user_id] = time.time()

    await group_memory.save_message(
        0, settings.bot_qq_id, personality.name, reply, is_bot=True
    )
    await relationship.update_closeness(event.user_id, 0.02)


@app.websocket("/onebot/ws")
async def onebot_ws(websocket: WebSocket):
    await websocket.accept()
    logger.info("NapCat WebSocket connected")

    try:
        while True:
            data = await websocket.receive_json()

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
