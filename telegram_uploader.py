"""
Telegram Uploader Module using Pyrofork (MTProto)
Supports uploading large videos (up to 2GB) to Telegram channels or chats
using Bot Token, API ID, and API Hash.
"""

import os
import time
import json
import logging
import asyncio
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Union

import requests

logger = logging.getLogger("telegram_uploader")

PYROGRAM_IMPORT_ERROR = None
try:
    from pyrogram import Client
    PYROGRAM_AVAILABLE = True
except Exception as e:
    try:
        from pyrofork import Client  # fallback check
        PYROGRAM_AVAILABLE = True
    except Exception as e2:
        Client = None
        PYROGRAM_AVAILABLE = False
        PYROGRAM_IMPORT_ERROR = str(e2)
        logger.warning(f"Pyrofork/Pyrogram not installed: {e2}")


def format_bytes(bytes_count: Optional[float]) -> str:
    """Format bytes into human-readable string."""
    if not bytes_count or bytes_count <= 0:
        return "N/A"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_count < 1024.0:
            return f"{bytes_count:.1f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.1f} PB"


def validate_telegram_credentials(bot_token: str) -> Dict[str, Any]:
    """
    Quickly verifies a Telegram Bot Token via HTTP Bot API getMe.
    Returns bot info (id, first_name, username) or error message.
    """
    bot_token = bot_token.strip() if bot_token else ""
    if not bot_token or ":" not in bot_token:
        return {"valid": False, "error": "Invalid Bot Token format (e.g. 123456:ABC-DEF...)"}

    try:
        url = f"https://api.telegram.org/bot{bot_token}/getMe"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("ok"):
            result = data.get("result", {})
            return {
                "valid": True,
                "bot_id": result.get("id"),
                "first_name": result.get("first_name"),
                "username": result.get("username")
            }
        else:
            return {"valid": False, "error": data.get("description", "Unauthorized Bot Token")}
    except Exception as e:
        return {"valid": False, "error": f"Network error checking bot token: {str(e)}"}


def get_video_metadata(file_path: str) -> Dict[str, Any]:
    """Extracts duration, width, and height using ffprobe if available."""
    meta = {"duration": 0, "width": 1280, "height": 720}
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=width,height,duration",
            "-of", "json", file_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            format_info = data.get("format", {})
            if "duration" in format_info:
                meta["duration"] = int(float(format_info["duration"]))
            for stream in data.get("streams", []):
                if stream.get("width") and stream.get("height"):
                    meta["width"] = int(stream["width"])
                    meta["height"] = int(stream["height"])
                    if not meta["duration"] and stream.get("duration"):
                        meta["duration"] = int(float(stream["duration"]))
                    break
    except Exception as e:
        logger.debug(f"ffprobe metadata extraction skipped: {e}")
    return meta


def generate_video_thumbnail(file_path: str, thumb_path: str) -> bool:
    """Generates a 320px JPEG thumbnail screenshot at 2 seconds using ffmpeg."""
    try:
        cmd = [
            "ffmpeg", "-y", "-ss", "00:00:02", "-i", file_path,
            "-vframes", "1", "-vf", "scale=320:-1", thumb_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        return res.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0
    except Exception as e:
        logger.debug(f"ffmpeg thumbnail generation skipped: {e}")
        return False


def parse_chat_id(chat_id_raw: Union[str, int]) -> Union[int, str]:
    """Parses chat ID to proper int or username string for Pyrogram."""
    if isinstance(chat_id_raw, int):
        return chat_id_raw
    s = str(chat_id_raw).strip()
    # Check numeric (e.g. -1001234567890 or 12345678)
    if s.startswith("-100") or s.lstrip("-").isdigit():
        return int(s)
    # Check username
    if not s.startswith("@") and not s.isdigit():
        s = f"@{s}"
    return s


async def upload_video_to_telegram(
    file_path: str,
    caption: str,
    bot_token: str,
    api_id: Union[int, str],
    api_hash: str,
    chat_id: Union[str, int],
    progress_callback: Optional[Callable[[float, str], None]] = None
) -> Dict[str, Any]:
    """
    Uploads a video to Telegram via Pyrofork MTProto client (supports up to 2GB).
    Runs with in_memory=True (no persistent session files on disk).
    """
    if not PYROGRAM_AVAILABLE:
        return {
            "success": False,
            "error": "Pyrofork is not installed. Please run: pip install pyrofork tgcrypto"
        }

    file_p = Path(file_path)
    if not file_p.exists() or file_p.stat().st_size == 0:
        return {"success": False, "error": f"File does not exist on disk: {file_path}"}

    try:
        api_id_int = int(str(api_id).strip())
    except ValueError:
        return {"success": False, "error": f"Invalid Telegram API ID '{api_id}'. It must be an integer."}

    api_hash_clean = str(api_hash).strip()
    bot_token_clean = str(bot_token).strip()
    target_chat = parse_chat_id(chat_id)

    # 1. Extract metadata and thumbnail
    meta = get_video_metadata(str(file_p))
    thumb_path = str(file_p.with_suffix(".thumb.jpg"))
    has_thumb = generate_video_thumbnail(str(file_p), thumb_path)

    # 2. Progress wrapper
    start_time = time.time()
    last_update = [0.0]

    async def pyrogram_progress(current, total):
        now = time.time()
        # Update progress at most every 0.8 seconds
        if now - last_update[0] >= 0.8 or current == total:
            last_update[0] = now
            pct = round((current / total) * 100, 1) if total > 0 else 0.0
            elapsed = now - start_time
            speed = current / elapsed if elapsed > 0 else 0
            speed_str = f"{format_bytes(speed)}/s"
            if progress_callback:
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(pct, speed_str)
                else:
                    progress_callback(pct, speed_str)

    # 3. Create temporary in-memory client
    session_name = f"omni_uploader_{int(time.time() * 1000) % 100000}"
    client = Client(
        name=session_name,
        api_id=api_id_int,
        api_hash=api_hash_clean,
        bot_token=bot_token_clean,
        in_memory=True
    )

    try:
        logger.info(f"Connecting to Telegram MTProto for chat: {target_chat}...")
        await client.start()

        logger.info(f"Uploading video {file_p.name} ({format_bytes(file_p.stat().st_size)}) to Telegram...")
        clean_caption = caption[:1024] if caption else file_p.name  # Telegram max caption length 1024

        sent_msg = await client.send_video(
            chat_id=target_chat,
            video=str(file_p),
            caption=clean_caption,
            duration=meta.get("duration", 0),
            width=meta.get("width", 1280),
            height=meta.get("height", 720),
            thumb=thumb_path if has_thumb and os.path.exists(thumb_path) else None,
            supports_streaming=True,
            progress=pyrogram_progress
        )

        # 4. Generate view link if possible
        tg_link = None
        if sent_msg:
            if sent_msg.chat and sent_msg.chat.username:
                tg_link = f"https://t.me/{sent_msg.chat.username}/{sent_msg.id}"
            elif sent_msg.chat and str(sent_msg.chat.id).startswith("-100"):
                clean_cid = str(sent_msg.chat.id).replace("-100", "")
                tg_link = f"https://t.me/c/{clean_cid}/{sent_msg.id}"

        logger.info(f"Telegram upload succeeded! Message ID: {sent_msg.id}")
        return {
            "success": True,
            "message_id": sent_msg.id,
            "url": tg_link,
            "chat_id": str(target_chat)
        }

    except Exception as e:
        logger.error(f"Telegram upload failed: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }
    finally:
        try:
            if client.is_connected:
                await client.stop()
        except Exception:
            pass
        # Clean up temporary thumbnail
        if has_thumb and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass
