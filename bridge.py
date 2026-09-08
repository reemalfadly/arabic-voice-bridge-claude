# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "claude-agent-sdk>=0.2.0",
#     "httpx>=0.27",
#     "faster-whisper>=1.0",
# ]
# ///
"""
Telegram <-> Claude bridge ("secretary bot")
============================================

A long-running process that lets you chat with Claude through a Telegram bot:

  * You send a message to the bot  -> it is handed to a persistent Claude agent
    (full tools: read/write files, run commands, search the web, and the
    telegram MCP server for sending files/photos back to you).
  * Claude's reply is sent back to the same Telegram chat.
  * Send a file/photo to the bot -> it is saved into the workspace and Claude
    is told where it is.

SECURITY
--------
The bot answers ONLY the chat id in TELEGRAM_ALLOWED_CHAT_ID. Every other
sender is ignored. The agent runs with permission_mode="bypassPermissions"
(no confirmation prompts) and can run shell commands on this machine, so the
allow-list is the only thing standing between "my secretary" and "anyone who
finds the bot gets a shell". Keep the bot token secret; if it leaks, revoke it
in @BotFather immediately.

Environment variables:
  TELEGRAM_BOT_TOKEN        (required)
  TELEGRAM_ALLOWED_CHAT_ID  (required)  only this chat is served
  BRIDGE_WORKSPACE          (optional)  working dir for the agent
                                        default: C:\\Users\\engre\\Downloads
  BRIDGE_MODEL              (optional)  model override, e.g. "claude-sonnet-5"

Run:
  uv run --script bridge.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import traceback
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import httpx
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

API = "https://api.telegram.org"
HERE = Path(__file__).resolve().parent

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
WORKSPACE = Path(os.environ.get("BRIDGE_WORKSPACE", r"C:\Users\engre\Downloads")).expanduser()
MODEL = os.environ.get("BRIDGE_MODEL", "").strip() or None
UV = os.environ.get("BRIDGE_UV_PATH", "uv")
SERVER_PY = os.environ.get("TELEGRAM_MCP_SERVER", str(HERE / "server.py"))
OFFSET_FILE = HERE / ".bridge_offset"
INBOX = WORKSPACE / "telegram-inbox"

if not TOKEN or not ALLOWED_CHAT:
    sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID before running.")

# Single instance guard: two pollers on the same bot fight over getUpdates (409).
_SINGLETON = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _SINGLETON.bind(("127.0.0.1", 49517))
    _SINGLETON.listen(1)
except OSError:
    sys.exit("Another bridge instance is already running - nothing to do.")

SYSTEM_PROMPT = f"""You are a personal assistant reached over Telegram. The user talks to you
through a Telegram bot; your text replies are delivered to them as Telegram messages.

Rules:
- Keep replies short and plain. No markdown tables, no huge code dumps. Telegram
  messages are capped at 4096 characters.
- When the user asks you to SEND them a file, document, image, screenshot or any
  artifact, deliver it with the telegram MCP tools:
  mcp__telegram__send_telegram_file  (any file) or
  mcp__telegram__send_telegram_photo (images, inline preview).
  Do not pass chat_id; the server already targets the user's chat.
- Files the user sends you are saved under: {INBOX}
- Your working directory is: {WORKSPACE}
- You may read/write files and run shell commands without asking; act, then
  report what you did in one or two sentences.
"""

OPTIONS = ClaudeAgentOptions(
    cwd=str(WORKSPACE),
    model=MODEL,
    permission_mode="bypassPermissions",
    system_prompt=SYSTEM_PROMPT,
    allowed_tools=[
        "Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep",
        "WebSearch", "WebFetch", "TodoWrite", "NotebookEdit",
        "mcp__telegram__send_telegram_message",
        "mcp__telegram__send_telegram_file",
        "mcp__telegram__send_telegram_photo",
        "mcp__telegram__get_bot_info",
        "mcp__telegram__list_recent_chats",
    ],
    mcp_servers={
        "telegram": {
            "type": "stdio",
            "command": UV,
            "args": ["run", "--script", SERVER_PY],
            "env": {
                "TELEGRAM_BOT_TOKEN": TOKEN,
                "TELEGRAM_DEFAULT_CHAT_ID": ALLOWED_CHAT,
            },
        }
    },
)


# --------------------------------------------------------------------------- #
# Telegram helpers
# --------------------------------------------------------------------------- #
async def tg(client: httpx.AsyncClient, method: str, **params):
    r = await client.post(f"{API}/bot{TOKEN}/{method}", data=params)
    return r.json()


async def send(client: httpx.AsyncClient, text: str):
    for i in range(0, len(text) or 1, 4000):
        chunk = text[i : i + 4000] or "(فارغ)"
        await tg(client, "sendMessage", chat_id=ALLOWED_CHAT, text=chunk)


async def typing(client: httpx.AsyncClient):
    await tg(client, "sendChatAction", chat_id=ALLOWED_CHAT, action="typing")


async def download(client: httpx.AsyncClient, file_id: str, suggested: str) -> Path | None:
    info = await tg(client, "getFile", file_id=file_id)
    if not info.get("ok"):
        return None
    remote = info["result"]["file_path"]
    INBOX.mkdir(parents=True, exist_ok=True)
    dest = INBOX / (suggested or Path(remote).name or file_id)
    async with client.stream("GET", f"{API}/file/bot{TOKEN}/{remote}") as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            async for block in resp.aiter_bytes():
                fh.write(block)
    return dest


# --------------------------------------------------------------------------- #
# Speech-to-text (local, offline - faster-whisper)
# --------------------------------------------------------------------------- #
_whisper_model = None


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        print("Loading local speech-to-text model (first run downloads it, ~1-2 min)...")
        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        print("Speech-to-text model ready.")
    return _whisper_model


def transcribe_voice(path: Path) -> str:
    model = get_whisper_model()
    segments, _info = model.transcribe(str(path), language=None, vad_filter=True)
    return "".join(seg.text for seg in segments).strip()


def read_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def write_offset(v: int) -> None:
    try:
        OFFSET_FILE.write_text(str(v))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
async def ask_claude(agent: ClaudeSDKClient, prompt: str) -> str:
    await agent.query(prompt)
    parts: list[str] = []
    async for msg in agent.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    parts.append(block.text.strip())
        elif isinstance(msg, ResultMessage):
            if msg.is_error:
                return f"⚠️ صار خطأ: {msg.result or (msg.errors and msg.errors[0]) or 'unknown'}"
            if not parts and msg.result:
                parts.append(msg.result)
    return "\n\n".join(parts) or "(ما فيه رد)"


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
async def main() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    print(f"Bridge up. Workspace={WORKSPACE}  Chat={ALLOWED_CHAT}")

    async with httpx.AsyncClient(timeout=70.0) as http:
        me = await tg(http, "getMe")
        print("Bot:", me.get("result", {}).get("username"))
        await send(http, "🤖 السكرتير شغّال. كلّمني.")

        agent = ClaudeSDKClient(options=OPTIONS)
        await agent.connect()
        offset = read_offset()

        while True:
            try:
                upd = await tg(http, "getUpdates", offset=offset, timeout=50)
                if not upd.get("ok"):
                    await asyncio.sleep(3)
                    continue

                for item in upd["result"]:
                    offset = item["update_id"] + 1
                    write_offset(offset)
                    msg = item.get("message") or item.get("edited_message")
                    if not msg:
                        continue
                    if str(msg["chat"]["id"]) != ALLOWED_CHAT:
                        await tg(http, "sendMessage", chat_id=msg["chat"]["id"],
                                 text="غير مصرّح.")
                        continue

                    text = (msg.get("text") or msg.get("caption") or "").strip()

                    if text == "/reset":
                        await agent.disconnect()
                        agent = ClaudeSDKClient(options=OPTIONS)
                        await agent.connect()
                        await send(http, "🔄 بدأنا جلسة جديدة.")
                        continue
                    if text in ("/start", "/help"):
                        await send(http, "أرسل لي طلبك. أقدر أقرأ/أكتب ملفات، أشغّل أوامر، "
                                         "أبحث بالنت، وأرسل لك ملفات وصور. /reset لبدء جلسة جديدة.")
                        continue

                    saved: Path | None = None
                    is_voice = False
                    doc = msg.get("document")
                    photos = msg.get("photo")
                    voice = msg.get("voice")
                    audio = msg.get("audio")
                    if doc:
                        saved = await download(http, doc["file_id"], doc.get("file_name", ""))
                    elif photos:
                        big = photos[-1]
                        saved = await download(http, big["file_id"], f"photo_{msg['message_id']}.jpg")
                    elif voice:
                        is_voice = True
                        saved = await download(http, voice["file_id"], f"voice_{msg['message_id']}.ogg")
                    elif audio:
                        is_voice = True
                        saved = await download(http, audio["file_id"],
                                                audio.get("file_name") or f"audio_{msg['message_id']}.mp3")

                    if not text and not saved:
                        continue

                    prompt = text
                    if saved and is_voice:
                        await typing(http)
                        transcript = ""
                        try:
                            transcript = await asyncio.get_event_loop().run_in_executor(
                                None, transcribe_voice, saved
                            )
                        except Exception as e:
                            print("[bridge] STT failed:", e)
                            traceback.print_exc()
                        if transcript:
                            prompt = (
                                "استقبلت رسالة صوتية من المستخدم، وهذا تفريغها تلقائيًا (قد يحتوي "
                                f"أخطاء بسيطة بالتفريغ):\n\"{transcript}\"\n\n"
                                "ردّ عليه بناءً على كلامه مباشرة. إذا كان الكلام غير واضح أو ناقص "
                                "وضّح له بأدب إيش فهمت واسأله يوضح أكثر."
                            )
                        else:
                            prompt = (
                                f"استقبلت رسالة صوتية من المستخدم لكن ما قدرت أفرّغها (صوت غير واضح "
                                f"أو فشل التفريغ)، محفوظة هنا: {saved}\n"
                                "اعتذر له بإيجاز واطلب منه يعيد الإرسال أو يكتب اللي يبيه."
                            )
                    elif saved:
                        prompt = (f"استقبلت ملف من المستخدم ومحفوظ هنا: {saved}\n"
                                  f"{('طلب المستخدم: ' + text) if text else 'ما فيه نص مرفق؛ اسأل وش يبي منه.'}")

                    await typing(http)
                    try:
                        reply = await ask_claude(agent, prompt)
                    except Exception as e:  # keep the loop alive
                        reply = f"⚠️ خطأ داخلي: {e}"
                        traceback.print_exc()
                    await send(http, reply)

            except httpx.HTTPError as e:
                print("HTTP error:", e)
                await asyncio.sleep(3)
            except Exception:
                traceback.print_exc()
                await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
