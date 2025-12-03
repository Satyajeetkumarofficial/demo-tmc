#!/usr/bin/env python3
"""
blaze_thumb_bot.py — Normal Mode (strict thumbnail validation)
- Ensures thumbnail is JPEG and <= 200 KB (Telegram-friendly)
- Reuses video file_id (no big upload)
- Shows preview & clear errors if thumb invalid
"""
import os
import tempfile
import shutil
import subprocess
from pathlib import Path
import time
import traceback

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

# Required envs
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.error("Please set BOT_TOKEN, API_ID and API_HASH environment variables.")
    raise SystemExit(1)

# Optional config
# NOTE: Telegram reliably accepts thumbnails <= ~200 KB. We'll enforce 200 KB cap for upload.
TARGET_THUMB_KB = int(os.getenv("TARGET_THUMB_KB") or 2000)  # user preference (not exceeding 2000)
UPLOAD_THUMB_CAP_KB = 200  # enforce cap for actual upload (200 KB)
AGGRESSIVE = os.getenv("AGGRESSIVE_COMPRESSION", "0") == "1"
AUTO_DELETE = os.getenv("AUTO_DELETE", "0") == "1"

# Session path (use /tmp so container writable)
SESSION_NAME = "/tmp/thumb_bot"

# Create client
app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# In-memory pending map: chat_id -> Message (video message)
pending = {}

def run_ffmpeg_convert(src: str, dst: str, scale: str, q: int):
    """Run ffmpeg to convert image to JPEG with given scale & qscale."""
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-vf", f"scale={scale}:force_original_aspect_ratio=decrease",
        "-qscale:v", str(q),
        dst
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def make_jpeg_under(src: str, dst: str, target_kb: int, aggressive: bool=False) -> str:
    """
    Convert src to JPEG and try to make it <= target_kb kilobytes.
    Returns path to dst (JPEG). Raises RuntimeError if conversion fails.
    """
    src_p = Path(src)
    dst_p = Path(dst)

    # If already small JPEG, copy
    try:
        if src_p.suffix.lower() in (".jpg", ".jpeg") and src_p.stat().st_size <= target_kb * 1024:
            shutil.copy(str(src_p), str(dst_p))
            return str(dst_p)
    except Exception:
        pass

    # Initial conversion parameters
    if aggressive:
        scale = "min(854,iw):min(480,ih)"
        q = 5
    else:
        scale = "min(1280,iw):min(720,ih)"
        q = 3

    tmp = str(dst_p) + ".tmp.jpg"
    run_ffmpeg_convert(src, tmp, scale, q)

    # Iteratively reduce quality/resolution until fits
    quality = q
    resize_pass = 0
    while Path(tmp).exists() and Path(tmp).stat().st_size > target_kb * 1024 and quality <= 40:
        quality += 3
        # After certain passes, reduce resolution further
        resize_pass += 1
        if resize_pass == 3:
            # reduce resolution more aggressively
            scale = "min(640,iw):min(360,ih)"
        elif resize_pass >= 5:
            scale = "min(480,iw):min(270,ih)"

        # encode from tmp -> dst with lower quality
        run_ffmpeg_convert(tmp, str(dst_p), scale, quality)

        if Path(dst_p).exists() and Path(dst_p).stat().st_size <= target_kb * 1024:
            break

        # prepare next iteration: move dst back to tmp if exists
        if Path(dst_p).exists():
            shutil.move(str(dst_p), tmp)

    if Path(tmp).exists() and not Path(dst_p).exists():
        # try final move
        shutil.move(tmp, dst_p)

    if not Path(dst_p).exists():
        # fallback: try a single re-encode to JPEG using imagemagick (if ffmpeg failed) - but we assume ffmpeg exists.
        raise RuntimeError("Failed to create JPEG thumbnail")

    return str(dst_p)

@app.on_message(filters.command("start"))
async def cmd_start(_, m: Message):
    await m.reply_text("👋 Hello! Send a video, then send an image. I will apply the image as the video's thumbnail (Telegram requires JPG ≤ ~200KB).")

@app.on_message(filters.video)
async def on_video(_, m: Message):
    pending[m.chat.id] = m
    await m.reply_text("✅ Video received. Now send the image you want as the new thumbnail (prefer JPG ≤ 200 KB).")

@app.on_message(filters.photo | filters.document)
async def on_thumb(client: Client, m: Message):
    chat_id = m.chat.id
    if chat_id not in pending:
        return await m.reply_text("❌ No pending video. Send a video first.")

    video_msg = pending.pop(chat_id)
    status = await m.reply_text("⚙️ Processing thumbnail...")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            in_path = await client.download_media(m, file_name=f"{tmpdir}/thumb_in")
        except Exception as e:
            logger.exception("Failed to download incoming image: %s", e)
            await status.edit_text(f"❌ Failed to download the image: {e}")
            return

        out_path = f"{tmpdir}/thumb.jpg"

        # For upload we will enforce cap to UPLOAD_THUMB_CAP_KB (200 KB)
        target_kb_for_upload = min(TARGET_THUMB_KB, UPLOAD_THUMB_CAP_KB)

        # Try to convert/ensure JPEG under cap
        try:
            # First try with normal or aggressive based on flag
            make_jpeg_under(in_path, out_path, target_kb_for_upload, aggressive=AGGRESSIVE)
        except Exception as e:
            logger.warning("Primary conversion failed, trying aggressive: %s", e)
            try:
                # Try again aggressively
                make_jpeg_under(in_path, out_path, target_kb_for_upload, aggressive=True)
            except Exception as ex2:
                logger.exception("Aggressive conversion also failed: %s", ex2)
                # As last resort, send the preview & explain
                await client.send_photo(chat_id, in_path, caption="Couldn't convert this image to a suitable JPEG thumbnail (<= 200 KB). Please send a smaller image (JPEG).")
                await status.edit_text("❌ Could not create a valid thumbnail (server logs contain details). Please send a smaller JPG (<=200 KB).")
                return

        # Verify final size
        final_size_kb = Path(out_path).stat().st_size / 1024
        logger.info("Prepared thumbnail size: %.1f KB", final_size_kb)

        if final_size_kb > UPLOAD_THUMB_CAP_KB:
            # If still too large, show preview and ask user to resend smaller
            await client.send_photo(chat_id, out_path, caption=f"Thumbnail is still {final_size_kb:.1f} KB (> {UPLOAD_THUMB_CAP_KB} KB). Please send a smaller image.")
            await status.edit_text(f"❌ Thumbnail too large ({final_size_kb:.1f} KB). Send a smaller JPG ≤ {UPLOAD_THUMB_CAP_KB} KB.")
            return

        # Everything looks good — send video reusing file_id and uploading only small thumbnail
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
                    await client.delete_messages(chat_id=chat_id, message_ids=video_msg.message_id)
                except Exception:
                    pass

            await status.edit_text("✅ Done — video sent with new thumbnail.")
            return
        except Exception as e:
            tb = traceback.format_exc()
            logger.exception("Failed to send video with new thumbnail: %s\n%s", e, tb)
            # Send preview & error details (short) to user
            try:
                await client.send_photo(chat_id, out_path, caption="Preview of thumbnail I tried to use.")
            except Exception:
                pass
            await status.edit_text(f"❌ Failed to attach thumbnail: {e}. Check bot logs.")
            return

def start_bot():
    logger.info("Starting Normal Mode Thumb Bot (strict thumb)...")
    tries = 0
    max_retries = 2
    while tries < max_retries:
        try:
            app.start()
            logger.info("Bot started.")
            try:
                idle()  # block until termination
            finally:
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
