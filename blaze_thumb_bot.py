#!/usr/bin/env python3
"""
Blaze Instant Thumbnail / Cover Changer Bot — Single-file ready
Features:
- Reuses Telegram server-side video via file_id (no re-upload)
- Saved covers (MongoDB) optional
- Session stored in /tmp to avoid permission issues on containers
- Auto-retry on BadMsgNotification (msg_id/time sync)
- Inline menu, /save_cover, /covers, /stats, /cancel
"""

import os
import asyncio
import logging
import tempfile
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import BadMsgNotification
import motor.motor_asyncio
from bson.objectid import ObjectId

# Logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Env / config
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID") or 0) if os.getenv("OWNER_ID") else None
TARGET_THUMB_KB = int(os.getenv("TARGET_THUMB_KB") or 2000)  # in KB
AUTO_DELETE = os.getenv("AUTO_DELETE_ORIGINAL", "0") == "1"
AGGRESSIVE = os.getenv("AGGRESSIVE_COMPRESSION", "0") == "1"
PENDING_TIMEOUT = int(os.getenv("PENDING_TIMEOUT", 120))
SESSION_NAME = os.getenv("SESSION_NAME", "/tmp/blaze_thumb_bot")

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.error("BOT_TOKEN, API_ID and API_HASH are required environment variables.")
    raise SystemExit(1)

# sanitize target thumb
if TARGET_THUMB_KB <= 0:
    TARGET_THUMB_KB = 2000
if TARGET_THUMB_KB > 2000:
    logger.warning("TARGET_THUMB_KB > 2000KB may be rejected by Telegram. Capping to 2000KB.")
    TARGET_THUMB_KB = 2000

# Pyrogram client (session in /tmp)
app = Client(SESSION_NAME, bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# MongoDB (optional)
if MONGO_URI:
    mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
    db = mongo.get_default_database()
    logger.info("Connected to MongoDB")
else:
    db = None
    logger.warning("MONGO_URI not set — saved covers disabled.")

# pending map: chat_id -> {"video_msg": Message, "timer": Task}
pending: Dict[int, Dict[str, Any]] = {}

# Utilities
async def ensure_jpeg_and_size(src_path: str, dst_path: str, max_kb: int = 2000, aggressive: bool = False) -> str:
    """
    Convert image to JPEG and reduce size under max_kb if possible.
    Returns dst_path.
    """
    src = Path(src_path)
    dst = Path(dst_path)

    try:
        if src.suffix.lower() in (".jpg", ".jpeg") and src.stat().st_size <= max_kb * 1024:
            shutil.copy(str(src), str(dst))
            return str(dst)
    except Exception:
        pass

    tmp = str(dst) + ".tmp.jpg"
    # baseline resolution and quality; aggressive means smaller
    res = "min(1280,iw):min(720,ih)"
    q = 3
    if aggressive:
        res = "min(854,iw):min(480,ih)"
        q = 5

    cmd = ["ffmpeg", "-y", "-i", str(src), "-vf", f"scale={res}:force_original_aspect_ratio=decrease", "-qscale:v", str(q), tmp]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    quality = q
    while Path(tmp).exists() and Path(tmp).stat().st_size > max_kb * 1024 and quality <= 40:
        quality += 3
        cmd = ["ffmpeg", "-y", "-i", tmp, "-qscale:v", str(quality), str(dst)]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if Path(dst).exists() and Path(dst).stat().st_size <= max_kb * 1024:
            break
        if Path(dst).exists():
            shutil.move(str(dst), tmp)

    if Path(tmp).exists() and not Path(dst).exists():
        shutil.move(tmp, dst)

    if not Path(dst).exists():
        shutil.copy(str(src), str(dst))

    return str(dst)

# DB helpers
async def save_cover_to_db(owner_id: int, name: str, file_id: str) -> Optional[str]:
    if not db:
        return None
    doc = {"owner_id": owner_id, "name": name, "file_id": file_id}
    res = await db.covers.insert_one(doc)
    return str(res.inserted_id)

async def list_covers(owner_id: int) -> List[Dict[str, Any]]:
    if not db:
        return []
    return await db.covers.find({"owner_id": owner_id}).to_list(length=200)

async def get_cover(owner_id: int, cid: str) -> Optional[Dict[str, Any]]:
    if not db:
        return None
    doc = await db.covers.find_one({"_id": ObjectId(cid), "owner_id": owner_id})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc

async def delete_cover(owner_id: int, cid: str) -> bool:
    if not db:
        return False
    res = await db.covers.delete_one({"_id": ObjectId(cid), "owner_id": owner_id})
    return res.deleted_count == 1

# Inline keyboards
def video_action_kbd():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Extract Metadata", callback_data="meta")],
        [InlineKeyboardButton("Extract Cover", callback_data="extract")],
        [InlineKeyboardButton("Set New Cover", callback_data="setcover")],
        [InlineKeyboardButton("Use Saved Cover", callback_data="use_saved")],
    ])

def covers_list_kbd(covers: List[Dict[str, Any]]):
    kb = []
    for c in covers:
        kb.append([InlineKeyboardButton(c["name"], callback_data=f"usecover:{c['_id']}"),
                   InlineKeyboardButton("Delete", callback_data=f"delcover:{c['_id']}")])
    return InlineKeyboardMarkup(kb) if kb else None

# Timeouts
async def pending_timeout(chat_id: int):
    await asyncio.sleep(PENDING_TIMEOUT)
    data = pending.get(chat_id)
    if data:
        try:
            await data["video_msg"].reply_text("Thumbnail timeout. Please send the video again.")
        except Exception:
            pass
        pending.pop(chat_id, None)

async def cleanup_pending(chat_id: int):
    data = pending.pop(chat_id, None)
    if not data: return
    task = data.get("timer")
    if task and not task.done():
        task.cancel()

# Handlers
@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    await message.reply_text(
        "Hello! 🔥 Blaze Thumbnail/ Cover Bot.\n\n"
        "Send a video — I'll ask for a cover image and apply it instantly (no re-upload of large video).\n\n"
        "Commands: /save_cover (reply to image), /covers, /cancel, /stats (owner)."
    )

@app.on_message(filters.video)
async def on_video(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id in pending:
        return await message.reply_text("A pending request already exists here. /cancel to abort.")
    pending[chat_id] = {"video_msg": message}
    pending[chat_id]["timer"] = asyncio.create_task(pending_timeout(chat_id))
    await message.reply_text("Please send me the new image to use as a cover.", reply_markup=video_action_kbd())

@app.on_message(filters.photo | filters.document)
async def on_thumbnail(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id not in pending:
        # maybe user is saving cover (handled in /save_cover)
        return

    video_msg: Message = pending[chat_id]["video_msg"]
    timer = pending[chat_id].get("timer")
    if timer and not timer.done():
        timer.cancel()

    status = await message.reply_text("Applying new cover and sending video...")

    with tempfile.TemporaryDirectory() as tmpdir:
        thumb_in = await client.download_media(message, file_name=os.path.join(tmpdir, "thumb_in"))
        thumb_out = os.path.join(tmpdir, "thumb.jpg")
        try:
            await ensure_jpeg_and_size(thumb_in, thumb_out, TARGET_THUMB_KB, AGGRESSIVE)
        except Exception:
            shutil.copy(thumb_in, thumb_out)

        caption = video_msg.caption or ""
        try:
            await client.send_video(
                chat_id=chat_id,
                video=video_msg.video.file_id,
                thumb=thumb_out,
                caption=caption,
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
            await status.edit_text("✅ Done — video sent with new cover.")
        except Exception as e:
            await status.edit_text(f"🚫 Failed: {e}")

    await cleanup_pending(chat_id)

# Save cover command
@app.on_message(filters.command("save_cover"))
async def cmd_save_cover(client: Client, message: Message):
    if not db:
        return await message.reply_text("Saved covers not available (MONGO_URI not configured).")
    reply = message.reply_to_message
    if not reply or not (reply.photo or reply.document):
        return await message.reply_text("Reply to a photo/document with /save_cover <name>")
    name = "cover" if len(message.text.split()) == 1 else message.text.split(None,1)[1].strip()
    file_id = reply.photo.file_id if reply.photo else reply.document.file_id
    cid = await save_cover_to_db(message.from_user.id, name, file_id)
    await message.reply_text(f"Saved cover '{name}'. ID: {cid}")

@app.on_message(filters.command("covers"))
async def cmd_covers(client: Client, message: Message):
    if not db:
        return await message.reply_text("Saved covers not available (MONGO_URI not configured).")
    covers = await list_covers(message.from_user.id)
    if not covers:
        return await message.reply_text("No saved covers. Reply to an image with /save_cover <name> to save.")
    kb = covers_list_kbd(covers)
    await message.reply_text("Your saved covers:", reply_markup=kb)

@app.on_callback_query()
async def cb_handler(client, cb):
    data = cb.data or ""
    user = cb.from_user
    chat_id = cb.message.chat.id if cb.message else None

    if data == "meta":
        pd = pending.get(chat_id)
        if not pd:
            await cb.answer("No video found. Send a video first.", show_alert=True)
            return
        v = pd["video_msg"].video
        txt = f"Duration: {v.duration}s\nSize: {v.file_size/1024/1024:.2f} MB\nResolution: {v.width}x{v.height}"
        await cb.answer()
        await cb.message.reply_text(txt)
        return

    if data == "extract":
        pd = pending.get(chat_id)
        if not pd:
            await cb.answer("No video found. Send a video first.", show_alert=True)
            return
        vmsg = pd["video_msg"]
        if getattr(vmsg.video, "thumb", None):
            try:
                await cb.message.reply_photo(vmsg.video.thumb.file_id, caption="Extracted cover.")
                await cb.answer()
            except Exception:
                await cb.answer("Failed to extract cover.", show_alert=True)
        else:
            await cb.answer("No embedded thumbnail found.", show_alert=True)
        return

    if data == "setcover":
        pd = pending.get(chat_id)
        if not pd:
            await cb.answer("No pending video. Send a video first.", show_alert=True)
            return
        await cb.answer("Reply to the chat with the image you want to use as a new cover.")
        return

    if data == "use_saved":
        if not db:
            await cb.answer("Saved covers not available.", show_alert=True)
            return
        covers = await list_covers(user.id)
        if not covers:
            await cb.answer("You have no saved covers.", show_alert=True)
            return
        kb = covers_list_kbd(covers)
        await cb.message.reply_text("Select a saved cover to use:", reply_markup=kb)
        await cb.answer()
        return

    if data.startswith("usecover:"):
        cover_id = data.split(":",1)[1]
        cover_doc = await get_cover(user.id, cover_id)
        if not cover_doc:
            await cb.answer("Cover not found or you do not own it.", show_alert=True)
            return
        pd = pending.get(chat_id)
        if not pd:
            await cb.answer("No pending video in this chat.", show_alert=True)
            return
        status = await cb.message.reply_text("Applying saved cover...")
        try:
            video_msg = pd["video_msg"]
            await client.send_video(
                chat_id=chat_id,
                video=video_msg.video.file_id,
                thumb=cover_doc["file_id"],
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
            await status.edit_text("✅ Done — sent video with saved cover.")
        except Exception as e:
            await status.edit_text(f"Failed: {e}")
        await cleanup_pending(chat_id)
        await cb.answer()
        return

    if data.startswith("delcover:"):
        cover_id = data.split(":",1)[1]
        ok = await delete_cover(user.id, cover_id)
        if ok:
            await cb.answer("Cover deleted.")
            await cb.message.edit_text("Cover deleted. Use /covers to view remaining.")
        else:
            await cb.answer("Failed to delete or not found.", show_alert=True)
        return

    await cb.answer()

@app.on_message(filters.command("cancel"))
async def cmd_cancel(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id in pending:
        await cleanup_pending(chat_id)
        await message.reply_text("Cancelled pending thumbnail request.")
    else:
        await message.reply_text("No pending request.")

@app.on_message(filters.command("stats"))
async def cmd_stats(client: Client, message: Message):
    if not OWNER_ID or message.from_user.id != OWNER_ID:
        return await message.reply_text("This command is for the bot owner only.")
    pending_count = len(pending)
    covers_count = 0
    if db:
        covers_count = await db.covers.count_documents({})
    await message.reply_text(f"Pending: {pending_count}\nSaved covers: {covers_count}\nTARGET_THUMB_KB={TARGET_THUMB_KB}\nAUTO_DELETE={AUTO_DELETE}\nAGGRESSIVE={AGGRESSIVE}")

# Graceful start with retry for BadMsgNotification
def start_bot():
    logger.info("Starting Blaze Thumb/ Cover Bot...")
    retries = 0
    max_retries = 2
    while retries < max_retries:
        try:
            app.start()
            logger.info("Bot started successfully.")
            app.idle()
            break
        except BadMsgNotification as e:
            logger.warning("BadMsgNotification (msg_id/time). Removing session and retrying... %s", e)
            # remove session files
            for suffix in ("", ".session"):
                try:
                    p = SESSION_NAME + suffix
                    if os.path.exists(p):
                        os.remove(p)
                        logger.info("Removed session file: %s", p)
                except Exception as ex:
                    logger.debug("Failed to remove %s: %s", p, ex)
            retries += 1
            time.sleep(2)
            continue
        except Exception as e:
            logger.exception("Unexpected error while starting: %s", e)
            break
    else:
        logger.error("Max retries reached; exiting.")

if __name__ == "__main__":
    start_bot()
