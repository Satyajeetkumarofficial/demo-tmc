#!/usr/bin/env python3
"""
blaze_thumb_bot.py — Normal Mode (fixed idle bug)
Usage:
  - Set env: BOT_TOKEN, API_ID, API_HASH
  - Optional env: TARGET_THUMB_KB (KB, default 2000), AGGRESSIVE_COMPRESSION=1, AUTO_DELETE=1
  - Run: python blaze_thumb_bot.py
"""
import os
import tempfile
import shutil
import subprocess
from pathlib import Path
import time

from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pyrogram.errors import BadMsgNotification

import logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Required envs
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.error("Please set BOT_TOKEN, API_ID and API_HASH environment variables.")
    raise SystemExit(1)

# Optional config
TARGET_THUMB_KB = int(os.getenv("TARGET_THUMB_KB") or 2000)  # in KB, max 2000 recommended
AGGRESSIVE = os.getenv("AGGRESSIVE_COMPRESSION", "0") == "1"
AUTO_DELETE = os.getenv("AUTO_DELETE", "0") == "1"

# Session path (use /tmp so container writable)
SESSION_NAME = "/tmp/thumb_bot"

# Create client
app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# In-memory pending map: chat_id -> Message (video message)
pending = {}

def compress_thumb_ffmpeg(src: str, dst: str, max_kb: int = 2000, aggressive: bool = False) -> str:
    """
    Convert/resize image to JPEG and try to make it <= max_kb.
    Returns dst path.
    Requires ffmpeg available in PATH.
    """
    src_p = Path(src)
    dst_p = Path(dst)

    try:
        if src_p.suffix.lower() in (".jpg", ".jpeg") and src_p.stat().st_size <= max_kb * 1024:
            shutil.copy(str(src_p), str(dst_p))
            return str(dst_p)
    except Exception:
        pass

    tmp = str(dst_p) + ".tmp.jpg"
    # choose resolution & initial quality
    if aggressive:
        scale = "min(854,iw):min(480,ih)"
        q = 5
    else:
        scale = "min(1280,iw):min(720,ih)"
        q = 3

    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vf", f"scale={scale}:force_original_aspect_ratio=decrease", "-qscale:v", str(q), tmp],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    quality = q
    # loop to reduce quality if still too large
    while Path(tmp).exists() and Path(tmp).stat().st_size > max_kb * 1024 and quality <= 40:
        quality += 3
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp, "-qscale:v", str(quality), str(dst)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if Path(dst).exists() and Path(dst).stat().st_size <= max_kb * 1024:
            break
        # move dst back to tmp for next iteration if exists
        if Path(dst).exists():
            shutil.move(str(dst), tmp)

    if Path(tmp).exists() and not Path(dst).exists():
        shutil.move(tmp, dst)

    # final fallback: copy original if nothing created
    if not Path(dst).exists():
        shutil.copy(src, dst)

    return str(dst)

@app.on_message(filters.command("start"))
async def cmd_start(_, m: Message):
    await m.reply_text("👋 Hello! Send a video, then send an image. I will attach the image as the video's thumbnail and send it back instantly.")

@app.on_message(filters.video)
async def on_video(_, m: Message):
    pending[m.chat.id] = m
    await m.reply_text("✅ Video received. Now send me the image you want as the new thumbnail.")

@app.on_message(filters.photo | filters.document)
async def on_thumb(client: Client, m: Message):
    chat_id = m.chat.id
    if chat_id not in pending:
        return await m.reply_text("❌ No pending video. Send a video first.")

    video_msg = pending.pop(chat_id)
    status = await m.reply_text("⚙️ Applying thumbnail...")

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = await client.download_media(m, file_name=f"{tmpdir}/thumb_in")
        out_path = f"{tmpdir}/thumb.jpg"

        # compress/convert to JPEG under TARGET_THUMB_KB
        try:
            compress_thumb_ffmpeg(in_path, out_path, max_kb=TARGET_THUMB_KB, aggressive=AGGRESSIVE)
        except Exception:
            # fallback: copy original
            shutil.copy(in_path, out_path)

        try:
            await client.send_video(
                chat_id,
                video=video_msg.video.file_id,  # reuse Telegram server-side file (no big upload)
                thumb=out_path,
                caption=video_msg.caption or "",
                supports_streaming=True,
                duration=video_msg.video.duration,
                width=video_msg.video.width,
                height=video_msg.video.height,
            )

            if AUTO_DELETE:
                try:
                    await client.delete_messages(chat_id, message_ids=video_msg.message_id)
                except Exception:
                    pass

            await status.edit_text("✅ Done — video sent with new thumbnail.")
        except Exception as e:
            await status.edit_text(f"❌ Failed to send video with new thumbnail: {e}")

def start_bot():
    logger.info("Starting Normal Mode Thumb Bot...")
    tries = 0
    max_retries = 2
    while tries < max_retries:
        try:
            app.start()
            logger.info("Bot started.")
            try:
                idle()  # blocks until termination signal
            finally:
                # ensure client stops cleanly
                try:
                    app.stop()
                except Exception:
                    pass
            break
        except BadMsgNotification:
            logger.warning("BadMsgNotification: removing session and retrying...")
            for ext in ("", ".session"):
                p = SESSION_NAME + ext
                try:
                    if os.path.exists(p):
                        os.remove(p)
                        logger.info("Removed session file: %s", p)
                except Exception:
                    pass
            tries += 1
            time.sleep(1)
        except Exception as e:
            logger.exception("Unexpected start error: %s", e)
            break
    else:
        logger.error("Unable to start bot after retries.")

if __name__ == "__main__":
    start_bot()
