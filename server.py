# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp[cli]>=2.0",
#     "httpx>=0.27",
# ]
# ///
"""
Telegram MCP server
===================

A small Model Context Protocol server that exposes the Telegram Bot API so an
MCP client (Claude Desktop, Claude Code, ...) can send messages, documents and
photos to a Telegram chat.

Configuration (environment variables):
    TELEGRAM_BOT_TOKEN        (required)  Token from @BotFather, e.g. "123456:ABC-DEF..."
    TELEGRAM_DEFAULT_CHAT_ID  (optional)  Default chat id used when a tool call
                                          does not pass `chat_id` explicitly.

Run directly for a quick check:
    uv run --script server.py --selfcheck
"""

from __future__ import annotations

import os
import sys
import mimetypes
from pathlib import Path
from typing import Any

import httpx
from mcp.server import MCPServer

API_ROOT = "https://api.telegram.org"

# Telegram Bot API upload ceiling for bots is 50 MB.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

mcp = MCPServer("telegram")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather, then set "
            "the token in the MCP server's `env` config."
        )
    return token


def _resolve_chat_id(chat_id: str | int | None) -> str:
    if chat_id is not None and str(chat_id).strip() != "":
        return str(chat_id).strip()
    env_chat = os.environ.get("TELEGRAM_DEFAULT_CHAT_ID", "").strip()
    if env_chat:
        return env_chat
    raise RuntimeError(
        "No chat_id given and TELEGRAM_DEFAULT_CHAT_ID is not set. Pass `chat_id`, "
        "or run the `list_recent_chats` tool to discover your chat id (send a "
        "message to the bot first)."
    )


async def _call(method: str, *, data: dict[str, Any] | None = None,
                files: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{API_ROOT}/bot{_token()}/{method}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, data=data, files=files)
    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError(f"Telegram returned non-JSON (HTTP {resp.status_code}): {resp.text[:400]}")

    if not payload.get("ok"):
        desc = payload.get("description", "unknown error")
        code = payload.get("error_code", resp.status_code)
        hint = ""
        if code == 401:
            hint = " -> the bot token looks invalid."
        elif code == 400 and "chat not found" in desc.lower():
            hint = " -> the chat_id is wrong, or the user/group has never started the bot."
        elif code == 403:
            hint = " -> the bot is blocked by the user or was removed from the group."
        raise RuntimeError(f"Telegram API error {code}: {desc}{hint}")

    return payload["result"]


def _check_file(file_path: str) -> Path:
    p = Path(file_path).expanduser()
    if not p.exists() or not p.is_file():
        raise RuntimeError(f"File not found: {p}")
    size = p.stat().st_size
    if size == 0:
        raise RuntimeError(f"File is empty: {p}")
    if size > MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"File is {size / 1_048_576:.1f} MB; the Telegram Bot API caps bot uploads at 50 MB."
        )
    return p


def _summarize(result: dict[str, Any]) -> dict[str, Any]:
    """Trim a Telegram Message object down to the useful bits."""
    out: dict[str, Any] = {"message_id": result.get("message_id"), "date": result.get("date")}
    chat = result.get("chat") or {}
    out["chat_id"] = chat.get("id")
    out["chat_title"] = chat.get("title") or chat.get("username") or chat.get("first_name")
    if "document" in result:
        out["document"] = {
            "file_name": result["document"].get("file_name"),
            "file_id": result["document"].get("file_id"),
            "file_size": result["document"].get("file_size"),
        }
    if "photo" in result:
        out["photo_sizes"] = len(result["photo"])
    return out


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
async def send_telegram_message(
    text: str,
    chat_id: str | None = None,
    parse_mode: str | None = None,
    disable_notification: bool = False,
    disable_link_preview: bool = False,
) -> dict[str, Any]:
    """Send a text message to a Telegram chat.

    Args:
        text: Message body (max 4096 characters).
        chat_id: Target chat id or @channelusername. Falls back to
            TELEGRAM_DEFAULT_CHAT_ID when omitted.
        parse_mode: Optional "MarkdownV2" or "HTML" to enable formatting.
        disable_notification: Send silently when true.
        disable_link_preview: Suppress the web page preview when true.
    """
    if len(text) > 4096:
        raise RuntimeError(f"text is {len(text)} chars; Telegram limit is 4096. Split it into parts.")
    data: dict[str, Any] = {
        "chat_id": _resolve_chat_id(chat_id),
        "text": text,
        "disable_notification": disable_notification,
        "link_preview_options": '{"is_disabled": true}' if disable_link_preview else None,
    }
    if parse_mode:
        data["parse_mode"] = parse_mode
    data = {k: v for k, v in data.items() if v is not None}
    return _summarize(await _call("sendMessage", data=data))


@mcp.tool()
async def send_telegram_file(
    file_path: str,
    chat_id: str | None = None,
    caption: str | None = None,
    parse_mode: str | None = None,
    disable_notification: bool = False,
) -> dict[str, Any]:
    """Send a local file to a Telegram chat as a document (any file type).

    Args:
        file_path: Absolute path to a file on this machine. Max 50 MB.
        chat_id: Target chat id or @channelusername. Falls back to
            TELEGRAM_DEFAULT_CHAT_ID when omitted.
        caption: Optional text shown under the file (max 1024 characters).
        parse_mode: Optional "MarkdownV2" or "HTML" for the caption.
        disable_notification: Send silently when true.
    """
    p = _check_file(file_path)
    data: dict[str, Any] = {
        "chat_id": _resolve_chat_id(chat_id),
        "disable_notification": disable_notification,
    }
    if caption:
        data["caption"] = caption[:1024]
    if parse_mode:
        data["parse_mode"] = parse_mode
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    with p.open("rb") as fh:
        files = {"document": (p.name, fh, mime)}
        return _summarize(await _call("sendDocument", data=data, files=files))


@mcp.tool()
async def send_telegram_photo(
    file_path: str,
    chat_id: str | None = None,
    caption: str | None = None,
    disable_notification: bool = False,
) -> dict[str, Any]:
    """Send a local image to a Telegram chat as a photo (compressed, inline preview).

    Use send_telegram_file instead to preserve the original file without recompression.

    Args:
        file_path: Absolute path to a .jpg/.png/.webp image on this machine. Max 10 MB.
        chat_id: Target chat id. Falls back to TELEGRAM_DEFAULT_CHAT_ID when omitted.
        caption: Optional text shown under the photo (max 1024 characters).
        disable_notification: Send silently when true.
    """
    p = _check_file(file_path)
    if p.stat().st_size > 10 * 1024 * 1024:
        raise RuntimeError("Photos are capped at 10 MB by Telegram; use send_telegram_file for larger images.")
    data: dict[str, Any] = {
        "chat_id": _resolve_chat_id(chat_id),
        "disable_notification": disable_notification,
    }
    if caption:
        data["caption"] = caption[:1024]
    with p.open("rb") as fh:
        files = {"photo": (p.name, fh)}
        return _summarize(await _call("sendPhoto", data=data, files=files))


@mcp.tool()
async def get_bot_info() -> dict[str, Any]:
    """Return the bot's own identity (getMe). Useful to verify the token works."""
    return await _call("getMe")


@mcp.tool()
async def list_recent_chats(limit: int = 20) -> list[dict[str, Any]]:
    """List distinct chats that recently messaged the bot, to discover chat ids.

    Send any message to your bot from the target chat first, then call this.
    Note: this consumes pending updates and won't work if a webhook is set.

    Args:
        limit: Max number of recent updates to scan (1-100).
    """
    result = await _call("getUpdates", data={"limit": max(1, min(limit, 100)), "timeout": 0})
    seen: dict[Any, dict[str, Any]] = {}
    for upd in result:
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if not chat or chat.get("id") in seen:
            continue
        seen[chat["id"]] = {
            "chat_id": chat.get("id"),
            "type": chat.get("type"),
            "title": chat.get("title"),
            "username": chat.get("username"),
            "name": " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or None,
        }
    if not seen:
        return [{"note": "No recent chats found. Send a message to the bot, then try again "
                         "(and make sure no webhook is configured)."}]
    return list(seen.values())


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        import asyncio

        async def _main() -> None:
            print("Token present:", bool(os.environ.get("TELEGRAM_BOT_TOKEN")))
            info = await _call("getMe")
            print("Bot OK:", info.get("username"))

        asyncio.run(_main())
    else:
        mcp.run(transport="stdio")
