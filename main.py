import asyncio
import subprocess
import sys
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional, List

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


# --- Session end callback ---
async def on_session_end():
    print("\n[Memory] Active session ended, summarizing...")
    rows = await db.fetchall(
        "SELECT qq_id, nickname FROM users WHERE message_count >= 1 "
        "ORDER BY last_seen DESC LIMIT 20"
    )
    for qq_id, nickname in rows:
        print(f"[Memory] Summarizing user: {nickname} ({qq_id})...")
        await user_file_memory.summarize_and_save(qq_id, nickname)
        print(f"[Memory] Saved: data/memory/{qq_id}.md")
    # Summarize group memory
    print(f"[Memory] Summarizing group {settings.target_group_id}...")
    await group_file_memory.summarize_and_save(settings.target_group_id)
    print("[Memory] Done.\n")


# --- Typing delay ---
async def typing_delay(text: str):
    """Simulate typing delay based on reply length."""
    delay = min(5.0, max(1.0, len(text) * 0.1))
    await asyncio.sleep(delay)


# --- Build context for LLM ---
async def build_user_context(user_id: int) -> str:
    user_ctx = await user_memory.get_user_summary(user_id)
    file_memory = user_file_memory.load(user_id)
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
    """Load group memory file."""
    return group_file_memory.load(group_id)


# --- Flush callback: batch decision for buffered messages ---
async def on_flush(messages: List[BufferedMessage], engagement: float):
    """Called when humanizer flushes buffered messages."""
    if not messages:
        return

    group_id = settings.target_group_id

    # Save all messages to memory first
    for msg in messages:
        await group_memory.save_message(
            group_id, msg.user_id, msg.nickname, msg.text
        )
        await user_memory.get_or_create_user(msg.user_id, msg.nickname)
        await user_memory.increment_message_count(msg.user_id)

    # Build context
    user_ctx = await build_user_context(messages[0].user_id)
    group_ctx_list = await group_memory.get_important_memories(group_id)
    group_file = build_group_context(group_id)
    if group_file:
        group_ctx_list.append(group_file)
    history = await build_chat_history(group_id)

    sys_prompt = personality.get_system_prompt(user_ctx, "\n".join(group_ctx_list))

    # Engagement influences LLM behavior
    if engagement > 70:
        sys_prompt += "\n\n你现在很积极，正在参与讨论。大部分消息都可以回复。"
    elif engagement > 30:
        sys_prompt += "\n\n你正在慢慢退出聊天，只回复有意思的消息或直接问你的。"
    else:
        sys_prompt += "\n\n你已经不太想聊了，除非有特别有意思的内容否则不回复。"

    # Prepare buffered messages for LLM
    buffered_data = [
        {
            "message_id": m.message_id,
            "nickname": m.nickname,
            "text": m.text,
            "is_at_bot": m.is_at_bot,
        }
        for m in messages
    ]

    # Let LLM decide
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
        await api_client.send_group_message(group_id, text, reply_to=reply_to)
        humanizer.notify_bot_replied()
        logger.info("[Bot][群聊] -> %s (quote=%s)", text[:60], quote)

        await group_memory.save_message(
            group_id, settings.bot_qq_id, personality.name, text, is_bot=True
        )

        for msg in messages:
            if msg.message_id == msg_id:
                await relationship.update_closeness(msg.user_id, 0.01)
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
    print("\nType 'help' for commands.\n")

    yield

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
    logger.info("[群聊][%s] %s (at=%s)", event.nickname, event.raw_text[:60], event.is_at_bot)
    recent_messages.append(event)

    # Buffer the message; returns immediately if @-mentioned
    pending = await humanizer.buffer_message(event)
    if pending is None:
        return  # buffered, will be processed later

    # Immediate reply (from @-mention)
    # Save to memory
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
        await api_client.send_group_message(event.group_id, text, reply_to=reply_to)
        humanizer.notify_bot_replied()
        logger.info("[Bot][群聊] -> %s (quote=%s)", text[:60], quote)

        await group_memory.save_message(
            event.group_id, settings.bot_qq_id, personality.name, text, is_bot=True
        )
        await relationship.update_closeness(event.user_id, 0.01)


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
    reply = await llm_client.generate_reply(sys_prompt, chat_history, event.raw_text)
    if not reply:
        return
    if personality.check_forbidden(reply):
        return

    reply = humanizer.post_process_reply(reply)
    await api_client.send_private_message(event.user_id, reply)
    humanizer.notify_bot_replied()
    logger.info("[Bot][私聊] -> %s", reply[:80])

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
  users                 List all users
  status                Show bot status
  user <qq_id>          Show user detail
  memory <qq_id>        Show user memory file
  group                 Show group memory file
  edit <qq_id> <f> <v>  Edit user field
  history <qq_id>       Show recent chat
  sessions              Show group memories
  summarize             Summarize all users + group now
  summarize <qq_id>     Summarize specific user
  summarize group       Summarize group memory
  help                  Show this help
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
        session_status = humanizer.get_session_status()
        print(f"\nStatus: {session_status}")
        print(f"Buffer: {len(humanizer._buffer)} messages")
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
        content = group_file_memory.load(settings.target_group_id)
        if content:
            print(f"\n=== Group memory ({settings.target_group_id}) ===\n{content}\n")
        else:
            print(f"No group memory file")

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
        gid = int(parts[1]) if len(parts) >= 2 else settings.target_group_id
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
            print(f"[Memory] Summarizing group {settings.target_group_id}...")
            await group_file_memory.summarize_and_save(settings.target_group_id)
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
            print(f"[Memory] Summarizing group...")
            await group_file_memory.summarize_and_save(settings.target_group_id)
            print(f"[Memory] Done. {len(rows)} users + group summarized.")

    else:
        print(f"Unknown: {action}. Type 'help'.")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765)
