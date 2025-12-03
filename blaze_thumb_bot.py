#!/usr/bin/env python3
"""
Blaze Instant Thumbnail / Cover Changer Bot
Features:
- Fast: reuses Telegram file_id for large videos (no re-upload)
- Saved covers in MongoDB (stores file_id, name, owner)
- Commands: /start, /save_cover, /covers, /stats, /cancel
- Inline menu similar to Blaze: Extract Metadata, Extract Cover & Thumbnail, Set New Cover (for this video), Use Saved Cover
- Auto-delete original (optional, env AUTO_DELETE_ORIGINAL=1)
- Adjustable TARGET_THUMB_KB (default 2000 KB)
- MongoDB via MONGO_URI env variable (recommended Atlas)
"""
import os
import asyncio
import logging
import tempfile
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, List

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import motor.motor_asyncio
from bson.objectid import ObjectId

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------- Config from env ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")
MONGO_URI = os.getenv("MONGO_URI")  # e.g. mongodb+srv://user:pass@cluster/mydb
OWNER_ID = int(os.getenv("OWNER_ID") or 0) if os.getenv("OWNER_ID") else None
TARGET_THUMB_KB = int(os.getenv("TARGET_THUMB_KB") or 2000)  # in KB, <= 2000 recommended
AUTO_DELETE = os.getenv("AUTO_DELETE_ORIGINAL", "0") == "1"
AGGRESSIVE = os.getenv("AGGRESSIVE_COMPRESSION", "0") == "1"
PENDING_TIMEOUT = int(os.getenv("PENDING_TIMEOUT", "120"))

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.error("Please set BOT_TOKEN, API_ID and API_HASH environment variables.")
    raise SystemExit(1)

if TARGET_THUMB_KB <= 0:
    TARGET_THUMB_KB = 2000
if TARGET_THUMB_KB > 2000:
    logger.warning("TARGET_THUMB_KB > 2000KB may be rejected by Telegram. Capping to 2000KB.")
    TARGET_THUMB_KB = 2000

# ---------- Pyrogram client ----------
app = Client("blaze_thumb_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ---------- MongoDB setup ----------
if not MONGO_URI:
    logger.warning("MONGO_URI not set. Saved covers feature will be disabled.")
    db = None
else:
    mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
    db = mongo.get_default_database()
    # collections: covers
    # cover doc: { _id, owner_id, name, file_id, created_at }
    logger.info("Connected to MongoDB")

# ---------- In-memory pending: chat_id -> {video_msg, timer}
pending: Dict[int, Dict[str, Any]] = {}

# ---------- Utilities ----------
async def ensure_jpeg_and_size(src_path: str, dst_path: str, max_kb: int = 2000, aggressive: bool = False) -> str:
    """Convert/resize to JPEG under max_kb. Returns dst_path."""
    src = Path(src_path)
    dst = Path(dst_path)

    try:
        if src.suffix.lower() in (".jpg", ".jpeg") and src.stat().st_size <= max_kb * 1024:
            shutil.copy(str(src), str(dst))
            return str(dst)
    except Exception:
        pass

    tmp = str(dst) + ".tmp.jpg"
    resolution = "min(1280,iw):min(720,ih)"
    qscale = 3
    if aggressive:
        resolution = "min(854,iw):min(480,ih)"
        qscale = 5

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vf", f"scale={resolution}:force_original_aspect_ratio=decrease",
        "-qscale:v", str(qscale),
        tmp
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    quality = qscale
    while Path(tmp).exists() and Path(tmp).stat().st_size > max_kb * 1024 and quality <= 40:
        quality += 3
        cmd = ["ffmpeg", "-y", "-i", tmp, "-qscale:v", str(quality), str(dst)]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if Path(dst).exists() and Path(dst).stat().st_size <= max_kb * 1024:
            break
        if Path(dst).exists():
            shutil.move(str(dst), tmp)

    if Path(tmp).exists() and not Path(dst).exists():
        shutil.move(str(tmp), str(dst))

    if not Path(dst).exists():
        shutil.copy(str(src), str(dst))
    return str(dst)

async def cleanup_pending(chat_id: int):
    data = pending.pop(chat_id, None)
    if not data:
        return
    task = data.get("timer")
    if task and not task.done():
        task.cancel()

async def pending_timeout(chat_id: int):
    await asyncio.sleep(PENDING_TIMEOUT)
    data = pending.get(chat_id)
    if data:
        try:
            await data["video_msg"].reply_text("Thumbnail timeout. Please send the video again to change its thumbnail.")
        except Exception:
            pass
        pending.pop(chat_id, None)

# ---------- DB helper functions ----------
async def save_cover_to_db(owner_id: int, name: str, file_id: str) -> Optional[str]:
    if not db:
        return None
    doc = {"owner_id": owner_id, "name": name, "file_id": file_id}
    res = await db.covers.insert_one(doc)
    return str(res.inserted_id)

async def list_covers(owner_id: int) -> List[Dict[str, Any]]:
    if not db:
        return []
    cursor = db.covers.find({"owner_id": owner_id})
    return await cursor.to_list(length=100)

async def get_cover(owner_id: int, cover_id: str) -> Optional[Dict[str, Any]]:
    if not db:
        return None
    doc = await db.covers.find_one({"_id": ObjectId(cover_id), "owner_id": owner_id})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc

async def delete_cover(owner_id: int, cover_id: str) -> bool:
    if not db:
        return False
    res = await db.covers.delete_one({"_id": ObjectId(cover_id), "owner_id": owner_id})
    return res.deleted_count == 1

# ---------- Inline keyboard helpers ----------
def video_action_kbd():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Extract Metadata", callback_data="meta")],
        [InlineKeyboardButton("Extract Cover & Thumbnail", callback_data="extract_cover")],
        [InlineKeyboardButton("Set New Cover (for this video)", callback_data="set_new_cover")],
        [InlineKeyboardButton("Use Saved Cover", callback_data="use_saved_cover")],
    ])

def covers_list_kbd(covers: List[Dict[str, Any]]):
    kb = []
    for c in covers:
        kb.append([InlineKeyboardButton(c["name"], callback_data=f"usecover:{c['_id']}"),
                   InlineKeyboardButton("Delete", callback_data=f"delcover:{c['_id']}")])
    return InlineKeyboardMarkup(kb) if kb else None

# ---------- Handlers ----------
@app.on_message(filters.private & filters.command("start"))
@app.on_message(filters.group & filters.command("start"))
async def cmd_start(client: Client, message: Message):
    await message.reply_text(
        "Hello! I am 🔥 Blaze thumbnail/cover changer bot.\n"
        "Send me a video to get started.\n\nCommands:\n"
        "/save_cover [name] — Reply to an image to save it. If no name given, it saves as default.\n"
        "/covers — Manage your saved covers.\n"
        "/stats — (owner) View usage statistics.",
        reply_markup=None
    )

@app.on_message(filters.private & filters.video)
@app.on_message(filters.group & filters.video)
async def on_video(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id in pending:
        await message.reply_text("There is already a pending request here. /cancel first or send the thumbnail.")
        return
    pending[chat_id] = {"video_msg": message}
    pending[chat_id]["timer"] = asyncio.create_task(pending_timeout(chat_id))
    await message.reply_text("Please send me the new image you would like to use as a cover.", reply_markup=video_action_kbd())

@app.on_message(filters.private & (filters.photo | filters.document))
@app.on_message(filters.group & (filters.photo | filters.document))
async def on_photo(client: Client, message: Message):
    chat_id = message.chat.id
    data = pending.get(chat_id)
    if not data:
        # If user replies to an earlier /setnew or wants to save cover, handle separately
        # If message is a reply to /save_cover, handled in command below.
        return

    video_msg: Message = data["video_msg"]
    # cancel timeout
    timer = data.get("timer")
    if timer and not timer.done():
        timer.cancel()

    status = await message.reply_text("Applying new cover and sending video...")

    # Work in temp dir only for thumb
    with tempfile.TemporaryDirectory() as tmpdir:
        thumb_in = await client.download_media(message, file_name=os.path.join(tmpdir, "thumb_in"))
        thumb_out = os.path.join(tmpdir, "thumb.jpg")
        try:
            await ensure_jpeg_and_size(thumb_in, thumb_out, max_kb=TARGET_THUMB_KB, aggressive=AGGRESSIVE)
        except Exception:
            shutil.copy(thumb_in, thumb_out)

        caption = video_msg.caption or ""
        try:
            video_file_id = video_msg.video.file_id
            sent = await client.send_video(
                chat_id=chat_id,
                video=video_file_id,
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
            await status.edit_text("✅ Done — video sent with new thumbnail.")
        except Exception as e:
            logger.exception("Failed to send video with new thumb: %s", e)
            await status.edit_text(f"Failed to send video with new thumbnail: {e}")

    await cleanup_pending(chat_id)

# ---------- Save cover command ----------
@app.on_message(filters.private & filters.command("save_cover"))
async def cmd_save_cover(client: Client, message: Message):
    # Usage: reply to an image with /save_cover [name]
    reply = message.reply_to_message
    if not reply or not (reply.photo or reply.document):
        await message.reply_text("Reply to a photo or image file with /save_cover <name> to save it.")
        return
    parts = message.text.split(maxsplit=1)
    name = parts[1].strip() if len(parts) > 1 else f"cover-{message.from_user.id}-{int(asyncio.get_event_loop().time())}"
    # download the photo? Better: store file_id so we don't need local storage
    file_id = None
    if reply.photo:
        # get highest quality photo
        file_id = reply.photo.file_id
    elif reply.document:
        # if it's an image document
        file_id = reply.document.file_id

    if not file_id:
        await message.reply_text("Could not identify the file to save.")
        return

    if not db:
        await message.reply_text("Saved covers feature is not available (MONGO_URI not configured).")
        return

    doc_id = await save_cover_to_db(message.from_user.id, name, file_id)
    await message.reply_text(f"Saved cover as '{name}'. ID: {doc_id}")

@app.on_message(filters.private & filters.command("covers"))
async def cmd_covers(client: Client, message: Message):
    if not db:
        await message.reply_text("Saved covers feature is not available (MONGO_URI not configured).")
        return
    covers = await list_covers(message.from_user.id)
    if not covers:
        await message.reply_text("No saved covers found. Reply to an image with /save_cover <name> to save one.")
        return
    kb = covers_list_kbd(covers)
    await message.reply_text("Your saved covers:", reply_markup=kb)

# ---------- Callbacks for inline buttons ----------
@app.on_callback_query()
async def cb_handler(client, cb):
    data = cb.data or ""
    user = cb.from_user
    chat_id = cb.message.chat.id if cb.message else None

    # meta
    if data == "meta":
        # metadata extraction: use last video in chat? Simpler: require user to have pending
        pd = pending.get(chat_id)
        if not pd:
            await cb.answer("No video found to extract metadata for. Send a video first.", show_alert=True)
            return
        v = pd["video_msg"].video
        txt = f"Filename: {pd['video_msg'].document.file_name if pd['video_msg'].document else 'N/A'}\nDuration: {v.duration}s\nSize: {v.file_size/1024/1024:.2f} MB\nResolution: {v.width}x{v.height}"
        await cb.answer()
        await cb.message.reply_text(txt)
        return

    if data == "extract_cover":
        pd = pending.get(chat_id)
        if not pd:
            await cb.answer("No video found. Send a video first.", show_alert=True)
            return
        vmsg = pd["video_msg"]
        # send existing thumbnail if present
        if getattr(vmsg.video, "thumb", None):
            try:
                await cb.message.reply_photo(vmsg.video.thumb.file_id, caption="Extracted cover/thumbnail.")
                await cb.answer()
            except Exception as e:
                await cb.answer("Failed to extract cover.", show_alert=True)
        else:
            await cb.answer("No embedded thumbnail found for this video.", show_alert=True)
        return

    if data == "set_new_cover":
        pd = pending.get(chat_id)
        if not pd:
            await cb.answer("No pending video. Send a video first.", show_alert=True)
            return
        await cb.answer("Reply to this chat with the image you want to use as a new cover.")
        return

    if data == "use_saved_cover":
        if not db:
            await cb.answer("Saved covers not available (no DB).", show_alert=True)
            return
        covers = await list_covers(user.id)
        if not covers:
            await cb.answer("You have no saved covers.", show_alert=True)
            return
        kb = covers_list_kbd(covers)
        await cb.message.reply_text("Select a saved cover to use:", reply_markup=kb)
        await cb.answer()
        return

    # usecover:<id>
    if data.startswith("usecover:"):
        cover_id = data.split(":", 1)[1]
        cover_doc = await get_cover(user.id, cover_id)
        if not cover_doc:
            await cb.answer("Cover not found or you do not own it.", show_alert=True)
            return
        # apply this cover to pending video if exists in chat
        pd = pending.get(chat_id)
        if not pd:
            await cb.answer("No pending video in this chat.", show_alert=True)
            return
        status = await cb.message.reply_text("Applying saved cover and sending video...")
        try:
            video_msg = pd["video_msg"]
            caption = video_msg.caption or ""
            await client.send_video(
                chat_id=chat_id,
                video=video_msg.video.file_id,
                thumb=cover_doc["file_id"],
                caption=caption,
                supports_streaming=True,
                duration=video_msg.video.duration,
                width=video_msg.video.width,
                height=video_msg.video.height
            )
            if AUTO_DELETE:
                try:
                    await client.delete_messages(chat_id=chat_id, message_ids=video_msg.message_id)
                except Exception:
                    pass
            await status.edit_text("✅ Done — sent video with saved cover.")
        except Exception as e:
            logger.exception("Error applying saved cover: %s", e)
            await status.edit_text(f"Failed: {e}")
        await cleanup_pending(chat_id)
        await cb.answer()
        return

    # delcover:<id>
    if data.startswith("delcover:"):
        cover_id = data.split(":", 1)[1]
        ok = await delete_cover(user.id, cover_id)
        if ok:
            await cb.answer("Cover deleted.")
            await cb.message.edit_text("Cover deleted. Use /covers to view remaining.")
        else:
            await cb.answer("Failed to delete or not found.", show_alert=True)
        return

    # fallback
    await cb.answer()

# ---------- Cancel command ----------
@app.on_message(filters.command("cancel"))
async def cmd_cancel(message: Message, client: Client):
    chat_id = message.chat.id
    if chat_id in pending:
        await cleanup_pending(chat_id)
        await message.reply_text("Cancelled pending thumbnail request.")
    else:
        await message.reply_text("No pending request.")

# ---------- Stats / admin ----------
@app.on_message(filters.private & filters.command("stats"))
async def cmd_stats(client: Client, message: Message):
    if not OWNER_ID or message.from_user.id != OWNER_ID:
        await message.reply_text("This command is for the bot owner only.")
        return
    pending_count = len(pending)
    covers_count = 0
    if db:
        covers_count = await db.covers.count_documents({})
    await message.reply_text(f"Pending requests: {pending_count}\nTotal saved covers: {covers_count}\nTARGET_THUMB_KB={TARGET_THUMB_KB}\nAUTO_DELETE={AUTO_DELETE}\nAGGRESSIVE={AGGRESSIVE}")

# ---------- Graceful shutdown ----------
@app.on_disconnect()
async def on_disconnect(client):
    logger.info("Bot disconnected.")

# ---------- Start ----------
if __name__ == "__main__":
    logger.info("Starting Blaze Thumb/ Cover Bot...")
    app.run()
