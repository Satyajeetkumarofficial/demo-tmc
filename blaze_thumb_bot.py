#!/usr/bin/env python3
"""
Blaze Instant Thumb Bot — FINAL FULL FIXED VERSION
Normal Mode (Fast + Stable + Thumbnail Guaranteed)
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

# -------- ENV --------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.error("BOT_TOKEN, API_ID, API_HASH required!")
    raise SystemExit(1)

TARGET_THUMB_KB = int(os.getenv("TARGET_THUMB_KB") or 2000)
UPLOAD_THUMB_CAP_KB = 200   # Telegram safe size
AGGRESSIVE = os.getenv("AGGRESSIVE_COMPRESSION", "0") == "1"
AUTO_DELETE = os.getenv("AUTO_DELETE", "0") == "1"

SESSION_NAME = "/tmp/thumb_bot"
app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

pending = {}

# ---------- UTIL FUNCTIONS ----------

def run_ffmpeg_convert(src: str, dst: str, scale: str, q: int):
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-vf", f"scale={scale}:force_original_aspect_ratio=decrease",
        "-qscale:v", str(q),
        dst
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_jpeg_under(src: str, dst: str, target_kb: int, aggressive: bool=False) -> str:
    src_p = Path(src)
    dst_p = Path(dst)

    try:
        if src_p.suffix.lower() in (".jpg", ".jpeg") and src_p.stat().st_size <= target_kb * 1024:
            shutil.copy(str(src_p), str(dst_p))
            return str(dst_p)
    except:
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
        raise RuntimeError("Could not produce JPEG")

    return str(dst_p)

# ---------- HANDLERS ----------

@app.on_message(filters.command("start"))
async def start_cmd(_, m: Message):
    await m.reply_text(
        "👋 Send a *video*, then send an *image*.\n"
        "I will instantly change the thumbnail (using server file_id).\n"
        "Recommended: JPG ≤ 200 KB."
    )


@app.on_message(filters.video)
async def video_handler(_, m: Message):
    pending[m.chat.id] = m
    await m.reply_text("✅ Video received.\nNow send the new thumbnail image.")


@app.on_message(filters.photo | filters.document)
async def thumb_handler(client: Client, m: Message):
    chat_id = m.chat.id

    if chat_id not in pending:
        return await m.reply_text("❌ Send video first.")

    video_msg = pending.pop(chat_id)
    status = await m.reply_text("⚙️ Preparing thumbnail...")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            in_path = await client.download_media(m, file_name=f"{tmpdir}/thumb_in")
        except Exception as e:
            await status.edit_text("❌ Could not download your image.")
            return

        out_path = f"{tmpdir}/thumb.jpg"
        target_kb = min(TARGET_THUMB_KB, UPLOAD_THUMB_CAP_KB)

        # --- STEP 1: normal/aggressive conversion ---
        success = False
        try:
            make_jpeg_under(in_path, out_path, target_kb, aggressive=AGGRESSIVE)
            success = True
        except:
            pass

        # --- STEP 2: forced re-encode (fallback) ---
        if not success:
            try:
                run_ffmpeg_convert(in_path, out_path, scale="iw:ih", q=25)
                if Path(out_path).exists():
                    if Path(out_path).stat().st_size <= target_kb * 1024:
                        success = True
                    else:
                        make_jpeg_under(out_path, out_path, target_kb, aggressive=True)
                        success = True
            except:
                pass

        # --- STEP 3: if still failed ---
        if not success:
            return await status.edit_text(
                "❌ Couldn't convert image to JPEG ≤200 KB.\n"
                "Please send a small JPG/PNG (not HEIC/WEBP)."
            )

        # --- size check ---
        final_kb = Path(out_path).stat().st_size / 1024
        if final_kb > UPLOAD_THUMB_CAP_KB:
            return await status.edit_text(
                f"❌ Thumbnail too large ({final_kb:.1f} KB).\n"
                f"Send JPG ≤ {UPLOAD_THUMB_CAP_KB} KB."
            )

        # --- SEND VIDEO WITH NEW THUMB ---
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
                except:
                    pass

            await status.edit_text("✅ Thumbnail updated successfully!")
            return

        except Exception as e:
            logger.exception("Video send failed: %s", e)
            return await status.edit_text(
                f"❌ Thumbnail apply failed: {e}\n"
                "Try a smaller JPG."
            )


# ---------- START BOT ----------
def start_bot():
    logger.info("🔥 Starting Blaze Thumb Bot (Final Mode)…")

    tries = 0
    while tries < 2:
        try:
            app.start()
            logger.info("Bot started.")
            idle()
            app.stop()
            break
        except BadMsgNotification:
            logger.warning("BadMsgNotification → deleting session and retrying")
            try:
                os.remove(SESSION_NAME + ".session")
            except:
                pass
            tries += 1
            time.sleep(1)
        except Exception as e:
            logger.exception("Startup Error: %s", e)
            break


if __name__ == "__main__":
    start_bot()
