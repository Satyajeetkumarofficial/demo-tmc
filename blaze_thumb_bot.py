#!/usr/bin/env python3
"""
Blaze Instant Thumb Bot — Full final single-file

Features:
- Instant thumbnail replace (send video -> send image -> bot resends same video with new thumbnail)
- /extract command (reply to a video OR run after sending video):
    1) sends embedded thumbnail (Telegram's tiny thumbnail) if present
    2) extracts a high-res frame from the video via ffmpeg and sends it as a normal photo (large MB)
- Robust ffmpeg fallbacks and user-friendly messages
- No DB, no saved-covers (keeps it simple & fast)
- Session stored in /tmp, BadMsgNotification retry, pyrogram idle
"""

import os
import tempfile
import shutil
import subprocess
import time
from pathlib import Path
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

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.error("BOT_TOKEN, API_ID, API_HASH required in env")
    raise SystemExit(1)

# Thumbnail size limits
TARGET_THUMB_KB = int(os.getenv("TARGET_THUMB_KB") or 2000)  # user preference (<=2000)
UPLOAD_THUMB_CAP_KB = int(os.getenv("UPLOAD_THUMB_CAP_KB") or 200)  # recommended to be ~200KB
AGGRESSIVE = os.getenv("AGGRESSIVE_COMPRESSION", "0") == "1"
AUTO_DELETE = os.getenv("AUTO_DELETE", "0") == "1"

# extraction time for frame (HH:MM:SS) default 00:00:01
EXTRACT_FRAME_TIME = os.getenv("EXTRACT_FRAME_TIME", "00:00:01")

# session path
SESSION_NAME = "/tmp/blaze_thumb_bot"

# create client
app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# in-memory pending video: chat_id -> Message
pending = {}

# ---------------- Utilities ----------------
def run_ffmpeg_convert(src: str, dst: str, scale: str, q: int):
    cmd = ["ffmpeg", "-y", "-i", src, "-vf", f"scale={scale}:force_original_aspect_ratio=decrease", "-qscale:v", str(q), dst]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def make_jpeg_under(src: str, dst: str, target_kb: int, aggressive: bool=False) -> str:
    """
    Convert `src` to JPEG at `dst` trying to make it <= target_kb KB.
    Raises RuntimeError on failure.
    """
    src_p = Path(src)
    dst_p = Path(dst)

    # quick copy if already JPG and small
    try:
        if src_p.suffix.lower() in (".jpg", ".jpeg") and src_p.stat().st_size <= target_kb * 1024:
            shutil.copy(str(src_p), str(dst_p))
            return str(dst_p)
    except Exception:
        pass

    if aggressive:
        scale = "min(854,iw):min(480,ih)"
        q = 5
    else:
        scale = "min(1280,iw):min(720,ih)"
        q = 3

    tmp = str(dst_p) + ".tmp.jpg"
    run_ffmpeg_convert(src, tmp, scale, q)

    quality = q
    resize_pass = 0
    while Path(tmp).exists() and Path(tmp).stat().st_size > target_kb * 1024 and quality <= 40:
        quality += 3
        resize_pass += 1
        if resize_pass == 3:
            scale = "min(640,iw):min(360,ih)"
        elif resize_pass >= 5:
            scale = "min(480,iw):min(270,ih)"

        run_ffmpeg_convert(tmp, str(dst_p), scale, quality)
        if Path(dst_p).exists() and Path(dst_p).stat().st_size <= target_kb * 1024:
            break
        if Path(dst_p).exists():
            shutil.move(str(dst_p), tmp)

    if Path(tmp).exists() and not Path(dst_p).exists():
        shutil.move(tmp, dst_p)

    if not Path(dst_p).exists():
        raise RuntimeError("Failed to produce JPEG thumbnail")

    return str(dst_p)

def extract_frame_from_video(video_path: str, out_path: str, timestamp: str = "00:00:01") -> None:
    """
    Extract a high-quality frame from the video at `timestamp` and save as JPEG `out_path`.
    """
    # -ss after -i gives accurate frame, but may be a bit slower
    cmd = ["ffmpeg", "-y", "-i", video_path, "-ss", timestamp, "-vframes", "1", "-q:v", "2", out_path]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ---------------- Handlers ----------------

@app.on_message(filters.command("start"))
async def cmd_start(_, m: Message):
    await m.reply_text(
        "🔥 Blaze Thumb Bot ready.\n"
        "Flow:\n"
        "1) Send a video\n"
        "2) Send an image to set as thumbnail (bot will resend video with new thumbnail instantly)\n\n"
        "Commands:\n"
        "/extract — reply to a video or run after sending a video to receive embedded tiny thumbnail + high-res extracted cover."
    )

@app.on_message(filters.video)
async def handler_video(_, m: Message):
    pending[m.chat.id] = m
    await m.reply_text("✅ Video received. Now send the image you want as thumbnail, or run /extract to get thumbnails/covers.")

@app.on_message(filters.photo | filters.document)
async def handler_image(client: Client, m: Message):
    chat_id = m.chat.id
    if chat_id not in pending:
        return await m.reply_text("❌ No pending video. Send a video first.")

    video_msg = pending.pop(chat_id)
    status = await m.reply_text("⚙️ Preparing thumbnail...")

    with tempfile.TemporaryDirectory() as td:
        try:
            in_path = await client.download_media(m, file_name=f"{td}/thumb_in")
        except Exception as e:
            logger.exception("download_media failed: %s", e)
            return await status.edit_text(f"❌ Failed to download image: {e}")

        out_path = f"{td}/thumb.jpg"
        target_for_upload = min(TARGET_THUMB_KB, UPLOAD_THUMB_CAP_KB)

        # Try conversion with best-effort fallbacks
        converted = False
        try:
            make_jpeg_under(in_path, out_path, target_for_upload, aggressive=AGGRESSIVE)
            converted = True
        except Exception:
            logger.warning("Primary conversion failed, will try fallback re-encode.")

        if not converted:
            try:
                # re-encode without scaling then aggressively try to reduce
                run_ffmpeg_convert(in_path, out_path, scale="iw:ih", q=25)
                if Path(out_path).exists() and Path(out_path).stat().st_size <= target_for_upload * 1024:
                    converted = True
                else:
                    make_jpeg_under(out_path, out_path, target_for_upload, aggressive=True)
                    converted = True
            except Exception as e:
                logger.exception("Fallback conversion failed: %s", e)
                converted = False

        if not converted:
            return await status.edit_text(
                "❌ Could not convert your image to a Telegram-friendly JPEG (≤ {} KB). Send a smaller JPG/PNG.".format(UPLOAD_THUMB_CAP_KB)
            )

        final_kb = Path(out_path).stat().st_size / 1024.0
        logger.info("Final thumb size: %.1f KB", final_kb)
        if final_kb > UPLOAD_THUMB_CAP_KB:
            return await status.edit_text(f"❌ Thumbnail still too large ({final_kb:.1f} KB). Send JPG ≤ {UPLOAD_THUMB_CAP_KB} KB.")

        # Send the video reusing server-side file_id and new thumbnail
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
            await status.edit_text(f"❌ Failed to send video with thumbnail: {e}")

# ---------- /extract command ----------
@app.on_message(filters.command("extract"))
async def cmd_extract(client: Client, m: Message):
    """
    Usage:
      - Reply /extract to a video message OR
      - Send a video, then run /extract (uses pending)
    Behavior:
      1) Sends the embedded thumbnail (video.video.thumb) if present
      2) Downloads the video, extracts a high-res frame at EXTRACT_FRAME_TIME, sends it as a normal photo (large)
    """
    # Prefer reply-to-video
    video_msg = None
    if m.reply_to_message and m.reply_to_message.video:
        video_msg = m.reply_to_message
    elif m.chat.id in pending:
        video_msg = pending[m.chat.id].get("video_msg") if isinstance(pending[m.chat.id], dict) else pending[m.chat.id]
    else:
        return await m.reply_text("Reply to a video with /extract or send a video then /extract (uses pending).")

    status = await m.reply_text("🔎 Extracting embedded thumbnail + high-res cover — please wait...")

    # 1) embedded thumbnail
    try:
        thumb = getattr(video_msg.video, "thumb", None)
        if thumb and getattr(thumb, "file_id", None):
            try:
                await client.send_photo(m.chat.id, thumb.file_id, caption="📌 Embedded thumbnail (tiny preview).")
            except Exception as e:
                logger.warning("Failed to send embedded thumb via file_id: %s", e)
                # fallback: attempt to download thumb and send
                try:
                    tmpd = tempfile.mkdtemp()
                    thumb_path = await client.download_media(thumb.file_id, file_name=f"{tmpd}/thumb.jpg")
                    if thumb_path:
                        await client.send_photo(m.chat.id, thumb_path, caption="📌 Embedded thumbnail (downloaded).")
                    shutil.rmtree(tmpd, ignore_errors=True)
                except Exception as ex2:
                    logger.exception("Failed to download+send embedded thumb: %s", ex2)
        else:
            await m.reply_text("No embedded thumbnail found in this video.")
    except Exception as e:
        logger.exception("Error sending embedded thumbnail: %s", e)

    # 2) extract high-res frame
    with tempfile.TemporaryDirectory() as td:
        video_file = os.path.join(td, "video.mp4")
        out_frame = os.path.join(td, "cover.jpg")
        try:
            # download full video (may be large)
            await status.edit_text("⬇️ Downloading video (may take time for large files)...")
            await client.download_media(video_msg, file_name=video_file)
        except Exception as e:
            logger.exception("download_media for extract failed: %s", e)
            return await status.edit_text(f"❌ Failed to download video for extraction: {e}")

        try:
            status = await status.edit_text(f"🖼️ Extracting frame at {EXTRACT_FRAME_TIME}...")
            extract_frame_from_video_fn = extract_frame_from_video  # local ref
            extract_frame_from_video(video_file, out_frame, timestamp=EXTRACT_FRAME_TIME)
        except Exception as e:
            logger.exception("Frame extraction failed: %s", e)
            return await status.edit_text(f"❌ Frame extraction failed: {e}")

        # send extracted high-res cover
        if os.path.exists(out_frame) and os.path.getsize(out_frame) > 0:
            try:
                await client.send_photo(m.chat.id, out_frame, caption=f"🖼️ Extracted cover at {EXTRACT_FRAME_TIME}")
                await status.edit_text("✅ Extraction complete.")
            except Exception as e:
                logger.exception("Failed to send extracted frame: %s", e)
                await status.edit_text(f"❌ Extraction succeeded but sending failed: {e}")
        else:
            await status.edit_text("❌ Extraction produced no image.")

# ---------------- Start/Run ----------------
def start_bot():
    logger.info("Starting Blaze Thumb Bot...")
    tries = 0
    max_retries = 2
    while tries < max_retries:
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
            logger.warning("BadMsgNotification -> deleting session and retrying.")
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
            logger.exception("Startup exception: %s", e)
            break

if __name__ == "__main__":
    start_bot()
