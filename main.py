import asyncio
import random
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
    await group_file_memory.summarize_and_save(group_id)
    print("[Memory] Done.\n")


# --- Proactive topic generation ---
_last_topic_time: Dict[int, float] = {}  # 上次开话题时间（防重复）


async def topic_loop():
    """活跃期内，安静一段时间后主动开话题。"""
    while True:
        try:
            await asyncio.sleep(60)  # 每分钟检查一次
            for gid in list(settings.group_ids):
                humanizer._check_random_session(gid)
                state = humanizer._get_state(gid)

                # 必须在活跃期中
                if state.session_active_until is None or time.time() >= state.session_active_until:
                    continue

                # 距离上次开话题至少 5 分钟
                last_topic = _last_topic_time.get(gid, 0)
                if time.time() - last_topic < 300:
                    continue

                # 上条消息是自己发的就不开（避免自言自语）
                last_msgs = await group_memory.get_recent_messages(gid, limit=1)
                if last_msgs and last_msgs[0].get("role") == "assistant":
                    continue

                # 开话题
                group_file = build_group_context(gid)
                history = await build_chat_history(gid, limit=10)
                group_ctx_list = await group_memory.get_important_memories(gid)
                if group_file:
                    group_ctx_list.append(group_file)

                topic = await llm_client.generate_topic(
                    personality.get_system_prompt("", "\n".join(group_ctx_list)),
                    history,
                )
                if not topic or personality.check_forbidden(topic):
                    continue

                topic = humanizer.post_process_reply(topic)
                await typing_delay(topic)
                await api_client.send_group_message(gid, topic)
                humanizer.notify_bot_replied(gid)
                _last_topic_time[gid] = time.time()
                logger.info("[Bot][群%d][主动] -> %s", gid, topic[:60])
                await group_memory.save_message(
                    gid, settings.bot_qq_id, personality.name, topic, is_bot=True
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Topic loop error: %s", e)


# --- Typing delay ---
async def typing_delay(text: str):
    """模拟打字延迟：每字0.3秒，范围3-10秒，随机0-1秒"""
    base = len(text) * 0.3
    delay = min(10.0, max(3.0, base)) + random.uniform(0, 1.0)
    await asyncio.sleep(delay)


def split_message(text: str) -> list[str]:
    """拆分长消息：按换行拆分，空行合并，每段不超过150字"""
    parts = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if len(line) > 150:
            for i in range(0, len(line), 150):
                parts.append(line[i:i+150])
        else:
            parts.append(line)
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


async def build_chat_history(group_id: int, limit: int = 15) -> list[dict]:
    raw_msgs = await group_memory.get_recent_messages(group_id, limit=limit)
    history = []
    for msg in raw_msgs:
        if msg["role"] == "assistant":
            history.append({"role": "assistant", "content": msg["content"]})
        else:
            history.append({"role": "user", "content": f'{msg["nickname"]}: {msg["content"]}'})
    return history


def build_group_context(group_id: int) -> str:
    """Load group memory file (sorted by importance, truncated)."""
    return group_file_memory.load_for_prompt(group_id)


# --- Flush callback: batch decision for buffered messages ---
async def on_flush(group_id: int, messages: List[BufferedMessage], engagement: float):
    """Called when humanizer flushes buffered messages."""
    if not messages:
        return

    for msg in messages:
        await group_memory.save_message(
            group_id, msg.user_id, msg.nickname, msg.text
        )
        await user_memory.get_or_create_user(msg.user_id, msg.nickname)
        await user_memory.increment_message_count(msg.user_id)

    user_ctx = await build_user_context(messages[0].user_id)
    group_ctx_list = await group_memory.get_important_memories(group_id)
    group_file = build_group_context(group_id)
    if group_file:
        group_ctx_list.append(group_file)
    history = await build_chat_history(group_id)

    sys_prompt = personality.get_system_prompt(user_ctx, "\n".join(group_ctx_list))

    fatigue = humanizer.get_fatigue(group_id)
    if engagement > 70:
        if fatigue > 60:
            sys_prompt += "\n\n你正在参与讨论，但已经有点累了。回复变短变敷衍，可以只回半句或表情。"
        else:
            sys_prompt += "\n\n你现在比较积极，可以参与讨论。但不要每条都回，选择有意思的回。"
    elif engagement > 30:
        sys_prompt += "\n\n你正在慢慢退出聊天，只回复直接问你的或者特别有意思的。可以敷衍。"
    else:
        sys_prompt += "\n\n你已经不太想聊了，除非有特别有意思的内容否则不回复。"

    buffered_data = [
        {
            "message_id": m.message_id,
            "nickname": m.nickname,
            "text": m.text,
            "is_at_bot": m.is_at_bot,
            "images": m.images,
        }
        for m in messages
    ]

    replies = await llm_client.decide_replies(sys_prompt, history, buffered_data)

    for reply in replies:
        text = reply.get("text", "")
        if not text:
            continue
        msg_id = reply.get("message_id", 0)
        quote = reply.get("quote", False)

        if personality.check_forbidden(text):
            continue

        text = humanizer.post_process_reply(text)
        reply_to = msg_id if quote else 0

        await typing_delay(text)
        await send_group_split(group_id, text, reply_to=reply_to)
        humanizer.notify_bot_replied(group_id)

        await group_memory.save_message(
            group_id, settings.bot_qq_id, personality.name, text, is_bot=True
        )

        for msg in messages:
            if msg.message_id == msg_id:
                await relationship.update_closeness(msg.user_id, 0.01)
                humanizer.record_replied_user(group_id, msg.user_id)
                break


# --- App lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global napcat_process, loop
    loop = asyncio.get_event_loop()
    await db.initialize()

    humanizer.set_session_end_callback(on_session_end)
    humanizer.set_flush_callback(on_flush)

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
    topic_task = asyncio.create_task(topic_loop())
    print("\nType 'help' for commands.\n")

    yield

    topic_task.cancel()

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
    logger.info("[群%d][%s] %s (at=%s)", event.group_id, event.nickname, event.raw_text[:60], event.is_at_bot)
    recent_messages.append(event)

    pending = await humanizer.buffer_message(event)
    if pending is None:
        return

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
    history = await build_chat_history(event.group_id)

    sys_prompt = personality.get_system_prompt(user_ctx, "\n".join(group_ctx_list))

    # For @-mention, use single-message decision
    buffered_data = [
        {
            "message_id": m.message_id,
            "nickname": m.nickname,
            "text": m.text,
            "is_at_bot": m.is_at_bot,
            "images": m.images,
        }
        for m in pending
    ]

    replies = await llm_client.decide_replies(sys_prompt, history, buffered_data)

    for reply in replies:
        text = reply.get("text", "")
        if not text:
            continue
        msg_id = reply.get("message_id", 0)
        quote = reply.get("quote", False)

        if personality.check_forbidden(text):
            continue

        text = humanizer.post_process_reply(text)
        reply_to = msg_id if quote else 0

        await typing_delay(text)
        await send_group_split(event.group_id, text, reply_to=reply_to)
        humanizer.notify_bot_replied(event.group_id, from_at=True)

        await group_memory.save_message(
            event.group_id, settings.bot_qq_id, personality.name, text, is_bot=True
        )
        await relationship.update_closeness(event.user_id, 0.01)
        humanizer.record_replied_user(event.group_id, event.user_id)


# --- Private message handler (unchanged, immediate reply) ---
async def handle_private_message(event: PrivateMessageEvent):
    logger.info("[私聊][%s] %s", event.nickname, event.raw_text[:80])

    await user_memory.get_or_create_user(event.user_id, event.nickname)
    await user_memory.increment_message_count(event.user_id)
    await group_memory.save_message(0, event.user_id, event.nickname, event.raw_text)

    user_ctx = await build_user_context(event.user_id)
    raw_msgs = await group_memory.get_recent_messages(0, limit=20)
    chat_history = []
    for msg in raw_msgs:
        if msg["role"] == "assistant":
            chat_history.append({"role": "assistant", "content": msg["content"]})
        else:
            chat_history.append({"role": "user", "content": msg["content"]})

    sys_prompt = personality.get_private_system_prompt(user_ctx)
    reply = await llm_client.generate_reply(
        sys_prompt, chat_history, event.raw_text,
        images=event.images if event.images else None,
    )
    if not reply:
        return
    if personality.check_forbidden(reply):
        return

    reply = humanizer.post_process_reply(reply)
    await typing_delay(reply)
    await send_private_split(event.user_id, reply)

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
  users                 列出所有用户
  status                查看当前状态（参与度、缓冲区、活跃期）
  user <qq_id>          查看用户详情
  memory <qq_id>        查看用户记忆文件
  group                 查看群记忆文件
  edit <qq_id> <f> <v>  编辑用户字段（nickname/personality_notes/interests/emotional_tendency/quirks）
  history <qq_id>       查看聊天记录
  sessions              查看群记忆数据库
  summarize             立即总结所有用户+群记忆
  summarize <qq_id>     总结指定用户
  summarize group       总结群记忆
  say <消息>            以bot身份发送群消息
  engage <0-100>        手动设置参与度
  session start [分钟]  手动开启活跃期（默认5分钟）
  session stop          手动结束活跃期
  buffer                查看当前消息缓冲区
  rel <qq_id>           查看与某人的关系
  rel <qq_a> <qq_b>     查看两人关系
  reload                热重载人格配置（不重启）
  test <消息>           测试LLM回复（不发送到群）
  debug                 切换详细日志模式
  help                  显示帮助
""")

    elif action == "users":
        rows = await db.fetchall(
            "SELECT qq_id, nickname, message_count, closeness_score, last_seen "
            "FROM users ORDER BY message_count DESC"
        )
        print(f"\n{'QQ':<15} {'Nick':<15} {'Msgs':<6} {'Closeness':<6} {'Last Seen'}")
        print("-" * 65)
        for r in rows:
            print(f"{r[0]:<15} {r[1]:<15} {r[2]:<6} {r[3]:<6.2f} {r[4]}")
        print(f"Total: {len(rows)}\n")

    elif action == "status":
        print(f"\nGroups: {settings.group_ids}")
        print(humanizer.get_session_status())
        print(f"DB: {settings.db_path}\n")

    elif action == "user" and len(parts) >= 2:
        qq_id = int(parts[1])
        row = await db.fetchone("SELECT * FROM users WHERE qq_id = ?", (qq_id,))
        if row:
            print(f"\n=== {row['nickname']} ({row['qq_id']}) ===")
            print(f"  personality: {row['personality_notes'] or '(empty)'}")
            print(f"  interests: {row['interests'] or '(empty)'}")
            print(f"  emotion: {row['emotional_tendency'] or '(empty)'}")
            print(f"  quirks: {row['quirks'] or '(empty)'}")
            print(f"  closeness: {row['closeness_score']:.2f}")
            print(f"  messages: {row['message_count']}")
            print(f"  first: {row['first_seen']}")
            print(f"  last: {row['last_seen']}\n")
        else:
            print(f"User {qq_id} not found")

    elif action == "memory" and len(parts) >= 2:
        qq_id = int(parts[1])
        content = user_file_memory.load(qq_id)
        if content:
            print(f"\n=== Memory: {qq_id} ===\n{content}\n")
        else:
            print(f"No memory file for {qq_id}")

    elif action == "group":
        gid = int(parts[1]) if len(parts) >= 2 else list(settings.group_ids)[0] if settings.group_ids else 0
        content = group_file_memory.load(gid)
        if content:
            print(f"\n=== Group memory ({gid}) ===\n{content}\n")
        else:
            print(f"No group memory file for {gid}")

    elif action == "edit" and len(parts) >= 4:
        qq_id = int(parts[1])
        field = parts[2]
        value = " ".join(parts[3:])
        allowed = {"nickname", "personality_notes", "interests", "emotional_tendency", "quirks"}
        if field in allowed:
            await db.execute(f"UPDATE users SET {field} = ? WHERE qq_id = ?", (value, qq_id))
            await db.commit()
            print(f"Updated {field} for {qq_id}")
        else:
            print(f"Allowed fields: {', '.join(allowed)}")

    elif action == "history" and len(parts) >= 2:
        qq_id = int(parts[1])
        rows = await db.fetchall(
            "SELECT nickname, content, is_bot, timestamp FROM chat_history "
            "WHERE user_id = ? ORDER BY timestamp DESC LIMIT 20",
            (qq_id,),
        )
        print(f"\n=== History: {qq_id} ===")
        for r in reversed(rows):
            tag = "[BOT]" if r[2] else "     "
            print(f"{r[3]} {tag} {r[0]}: {r[1][:80]}")
        print()

    elif action == "sessions":
        gid = int(parts[1]) if len(parts) >= 2 else list(settings.group_ids)[0] if settings.group_ids else 0
        rows = await db.fetchall(
            "SELECT memory_type, content, importance, created_at FROM group_memory "
            "WHERE group_id = ? ORDER BY importance DESC",
            (gid,),
        )
        print(f"\n=== Group memory ({gid}) ===")
        for r in rows:
            print(f"  [{r[0]}] {r[1][:80]}  ({r[3]})")
        if not rows:
            print("  (empty)")
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
            humanizer.trigger_active(gid, 3, engagement=level)
            print(f"Group {gid} engagement set to {level}")
        elif len(parts) >= 2:
            gid = int(parts[1])
            print(f"Group {gid} engagement: {humanizer.get_current_engagement(gid):.0f}")
        else:
            for gid in settings.group_ids:
                print(f"  Group {gid}: {humanizer.get_current_engagement(gid):.0f}")

    elif action == "session":
        if len(parts) >= 3 and parts[1] == "start":
            gid = int(parts[2])
            minutes = int(parts[3]) if len(parts) >= 4 else 5
            humanizer.trigger_active(gid, minutes, engagement=60)
            print(f"Group {gid} active for {minutes} min")
        elif len(parts) >= 3 and parts[1] == "stop":
            gid = int(parts[2])
            state = humanizer._get_state(gid)
            state.session_active_until = None
            state.engagement = 0
            state.buffer.clear()
            print(f"Group {gid} session stopped")
        elif len(parts) >= 2 and parts[1] == "start":
            # Default: first group
            gid = list(settings.group_ids)[0] if settings.group_ids else 0
            minutes = int(parts[2]) if len(parts) >= 3 else 5
            humanizer.trigger_active(gid, minutes, engagement=60)
            print(f"Group {gid} active for {minutes} min")
        elif len(parts) >= 2 and parts[1] == "stop":
            for gid in settings.group_ids:
                state = humanizer._get_state(gid)
                state.session_active_until = None
                state.engagement = 0
                state.buffer.clear()
            print("All sessions stopped")
        else:
            print("Usage: session start <群号> [分钟] | session stop <群号>")

    elif action == "buffer":
        if len(parts) >= 2:
            gid = int(parts[1])
            state = humanizer._get_state(gid)
            buf = state.buffer
            if buf:
                print(f"\nBuffer (group {gid}, {len(buf)} messages):")
                for m in buf:
                    at = " [@bot]" if m.is_at_bot else ""
                    print(f"  [{m.nickname}]{at}: {m.text[:60]}")
            else:
                print(f"Group {gid} buffer is empty")
        else:
            for gid in settings.group_ids:
                state = humanizer._get_state(gid)
                print(f"  Group {gid}: {len(state.buffer)} buffered")
        print()

    elif action == "rel":
        if len(parts) >= 3:
            a, b = int(parts[1]), int(parts[2])
            rel = await relationship.get_user_relationship(a, b)
            row_a = await db.fetchone("SELECT nickname FROM users WHERE qq_id = ?", (a,))
            row_b = await db.fetchone("SELECT nickname FROM users WHERE qq_id = ?", (b,))
            na = row_a[0] if row_a else str(a)
            nb = row_b[0] if row_b else str(b)
            if rel:
                print(f"\n{na} <-> {nb}: {rel['type']}")
                if rel['notes']:
                    print(f"  Notes: {rel['notes']}")
            else:
                print(f"\n{na} <-> {nb}: no relationship recorded")
            print()
        elif len(parts) >= 2:
            qq_id = int(parts[1])
            bot_rel = await relationship.get_bot_relationship(qq_id)
            row = await db.fetchone("SELECT nickname FROM users WHERE qq_id = ?", (qq_id,))
            name = row[0] if row else str(qq_id)
            print(f"\nBot <-> {name}: {bot_rel}")
            print()
        else:
            print("Usage: rel <qq_id> | rel <qq_a> <qq_b>")

    elif action == "reload":
        personality.__init__()
        print(f"Personality reloaded: {personality.name}")

    elif action == "test":
        if len(parts) >= 2:
            msg = " ".join(parts[1:])
            gid = list(settings.group_ids)[0] if settings.group_ids else 0
            user_ctx = ""
            group_ctx_list = await group_memory.get_important_memories(gid)
            group_file = build_group_context(gid)
            if group_file:
                group_ctx_list.append(group_file)
            history = await build_chat_history(gid)
            sys_prompt = personality.get_system_prompt(user_ctx, "\n".join(group_ctx_list))
            reply = await llm_client.generate_reply(sys_prompt, history, msg)
            if reply:
                print(f"\nLLM reply: {reply}\n")
            else:
                print("LLM returned empty")
        else:
            print("Usage: test <消息>")

    elif action == "debug":
        import logging
        lvl = logging.DEBUG if logger.level > logging.DEBUG else logging.INFO
        logger.setLevel(lvl)
        logging.getLogger("humanizer").setLevel(lvl)
        logging.getLogger("event_handler").setLevel(lvl)
        print(f"Log level: {'DEBUG' if lvl == logging.DEBUG else 'INFO'}")

    else:
        print(f"Unknown: {action}. Type 'help'.")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765)
