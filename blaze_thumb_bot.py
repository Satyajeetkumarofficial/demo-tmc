#!/usr/bin/env python3
"""
Simple Instant Thumb Changer — Minimal & Reliable

Flow:
1) Send a VIDEO -> bot replies "Send the image you want as thumbnail"
2) Send an IMAGE (photo or document) -> bot converts to JPEG (<= 200 KB recommended)
   and resends the SAME VIDEO (using file_id) with new thumbnail.

Requirements:
- ffmpeg installed in container (Dockerfile earlier)
- env: BOT_TOKEN, API_ID, API_HASH
"""
import os
import tempfile
import shutil
import subprocess
import time
from pathlib import Path

from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pyrogram.errors import BadMsgNotification
from threading import Thread
from flask import Flask

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive", 200

def run():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run).start()

import logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# --------- CONFIG ---------- (change if needed)
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.error("Please set BOT_TOKEN, API_ID and API_HASH environment variables.")
    raise SystemExit(1)

SESSION_NAME = os.getenv("SESSION_NAME", "/tmp/thumb_bot")
TARGET_THUMB_KB = int(os.getenv("TARGET_THUMB_KB") or 2000)   # internal attempt (KB)
UPLOAD_CAP_KB = int(os.getenv("UPLOAD_THUMB_CAP_KB") or 200)  # safe upload cap (KB)
AGGRESSIVE = os.getenv("AGGRESSIVE_COMPRESSION", "0") == "1"
AUTO_DELETE = os.getenv("AUTO_DELETE", "0") == "1"

# ----------------- CLIENT -----------------
app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# In-memory pending map: chat_id -> video_message
pending = {}

# ----------------- Helpers -----------------
def ffmpeg_encode_to_jpeg(src: str, dst: str, scale: str, q: int):
    cmd = ["ffmpeg", "-y", "-i", src, "-vf", f"scale={scale}:force_original_aspect_ratio=decrease", "-qscale:v", str(q), dst]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def convert_image_to_thumb(src: str, dst: str, target_kb: int, aggressive: bool=False) -> str:
    """
    Convert image `src` to JPEG dst trying to make it <= target_kb KB.
    Returns path to dst (raises RuntimeError on failure).
    """
    src_p = Path(src)
    dst_p = Path(dst)

    # quick-copy if already small jpeg
    try:
        if src_p.suffix.lower() in (".jpg", ".jpeg") and src_p.stat().st_size <= target_kb*1024:
            shutil.copy(str(src_p), str(dst_p))
            return str(dst_p)
    except Exception:
        pass

    scale = "min(1280,iw):min(720,ih)"
    q = 3
    if aggressive:
        scale = "min(854,iw):min(480,ih)"
        q = 5

    tmp = str(dst_p) + ".tmp.jpg"
    ffmpeg_encode_to_jpeg(src, tmp, scale, q)

    quality = q
    passes = 0
    while Path(tmp).exists() and Path(tmp).stat().st_size > target_kb*1024 and quality <= 40:
        quality += 3
        passes += 1
        if passes == 3:
            scale = "min(640,iw):min(360,ih)"
        elif passes >= 5:
            scale = "min(480,iw):min(270,ih)"
        ffmpeg_encode_to_jpeg(tmp, str(dst_p), scale, quality)
        if Path(dst_p).exists() and Path(dst_p).stat().st_size <= target_kb*1024:
            break
        if Path(dst_p).exists():
            shutil.move(str(dst_p), tmp)

    if Path(tmp).exists() and not Path(dst_p).exists():
        shutil.move(tmp, dst_p)

    if not Path(dst_p).exists():
        raise RuntimeError("Conversion failed")

    return str(dst_p)

# ----------------- Handlers -----------------

@app.on_message(filters.command("start"))
async def cmd_start(_, m: Message):
    await m.reply_text("Hello! Send me a video — I'll ask for the image to use as thumbnail.")

@app.on_message(filters.video)
async def on_video(_, m: Message):
    pending[m.chat.id] = m  # store the message object
    await m.reply_text("✅ Video received. Now send the image you want as the new thumbnail (send as photo or document).")

@app.on_message(filters.photo | filters.document)
async def on_image(client: Client, m: Message):
    chat_id = m.chat.id
    if chat_id not in pending:
        return await m.reply_text("❌ No pending video. Send a video first.")

    video_msg = pending.pop(chat_id)
    status = await m.reply_text("⚙️ Processing thumbnail...")

    with tempfile.TemporaryDirectory() as td:
        try:
            # download incoming image (photo/document)
            in_path = await client.download_media(m, file_name=f"{td}/in")
        except Exception as e:
            logger.exception("download_media failed: %s", e)
            return await status.edit_text(f"❌ Failed to download image: {e}")

        out_path = os.path.join(td, "thumb.jpg")
        try:
            convert_image_to_thumb(in_path, out_path, target_kb=min(TARGET_THUMB_KB, UPLOAD_CAP_KB), aggressive=AGGRESSIVE)
        except Exception as e:
            logger.warning("Primary conversion failed: %s", e)
            # fallback: try direct re-encode to jpeg once
            try:
                ffmpeg_encode_to_jpeg(in_path, out_path, "iw:ih", 25)
            except Exception as e2:
                logger.exception("Fallback conversion failed: %s", e2)
                return await status.edit_text("❌ Could not convert image to valid JPEG. Please send a JPG/PNG (smaller).")

        # verify size
        try:
            size_kb = Path(out_path).stat().st_size / 1024.0
        except Exception:
            size_kb = None

        if size_kb is None or size_kb > UPLOAD_CAP_KB:
            return await status.edit_text(f"❌ Thumbnail too large ({size_kb:.1f} KB). Please send smaller JPG ≤ {UPLOAD_CAP_KB} KB.")

        # finally send video using server file_id with new thumb
        try:
            await client.send_video(
                chat_id,
                video=video_msg.video.file_id,
                thumb=out_path,
                caption=video_msg.caption or "",
                supports_streaming=True,
                duration=video_msg.video.duration,
                width=video_msg.video.width,
                height=video_msg.video.height,
            )
            if AUTO_DELETE:
                try:
                    await video_msg.delete()
                except Exception:
                    pass
            await status.edit_text("✅ Done — video sent with new thumbnail.")
        except Exception as e:
            logger.exception("send_video failed: %s", e)
            await status.edit_text(f"❌ Failed to send video with new thumbnail: {e}")

# ----------------- run/idle -----------------
def start_bot():
    logger.info("Starting Instant Thumb Bot...")
    tries = 0
    while tries < 2:
        try:
            app.start()
            logger.info("Bot started.")
            try:
                idle()
            finally:
                try:
                    app.stop()
                except Exception:
                    pass
            break
        except BadMsgNotification:
            logger.warning("BadMsgNotification — removing session and retrying...")
            for ext in ("", ".session"):
                p = SESSION_NAME + ext
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            tries += 1
            time.sleep(1)
        except Exception as e:
            logger.exception("Startup error: %s", e)
            break

if __name__ == "__main__":
    start_bot()
