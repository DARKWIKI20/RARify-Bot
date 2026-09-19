import os
import re
import sys
import time
import math
import uuid
import shutil
import asyncio
import logging
from urllib.parse import unquote, urlparse
import aiohttp
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("RARBot")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
SPLIT_SIZE = os.getenv("SPLIT_SIZE", "1950m")
PORT = int(os.getenv("PORT", "8080"))

ALLOWED_USERS = set()
if ALLOWED_USERS_RAW:
    for uid in ALLOWED_USERS_RAW.split(","):
        uid_clean = uid.strip()
        if uid_clean.isdigit():
            ALLOWED_USERS.add(int(uid_clean))

if not API_ID or not API_HASH or not BOT_TOKEN:
    logger.error("Environment variables API_ID, API_HASH, and BOT_TOKEN are required.")
    sys.exit(1)

task_semaphore = asyncio.Semaphore(1)

app = Client(
    "rar_worker_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

def human_size(size_bytes: int) -> str:
    if not size_bytes:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    return f"{round(size_bytes / p, 2)} {units[i]}"

async def progress_callback(current, total, status_msg: Message, action_name: str, state: dict):
    now = time.time()
    if now - state.get("last_update", 0) < 4 and current != total:
        return
    state["last_update"] = now
    percent = (current / total) * 100 if total > 0 else 0
    filled = int(percent // 10)
    bar = "█" * filled + "░" * (10 - filled)
    text = (
        f"**Status:** {action_name}\n"
        f"[{bar}] {percent:.1f}%\n"
        f"{human_size(current)} / {human_size(total)}"
    )
    try:
        await status_msg.edit_text(text)
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass

def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS

async def extract_archive_if_needed(file_path: str, extract_to: str) -> bool:
    ext = os.path.splitext(file_path)[1].lower()
    archive_exts = {".zip", ".7z", ".tar", ".gz", ".bz2", ".xz", ".rar"}
    if ext in archive_exts or file_path.endswith(".tar.gz"):
        os.makedirs(extract_to, exist_ok=True)
        cmd = ["7z", "x", file_path, f"-o{extract_to}", "-y"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return proc.returncode == 0
    return False

async def run_rar_compression(input_target: str, output_rar_base: str) -> bool:
    # RAR flags:
    # a: add to archive
    # -m5: best compression
    # -md64m: 64MB dictionary size
    # -rr5p: 5% recovery record
    # -ep1: exclude base path from archive structure
    # -v: volume split
    cmd = [
        "rar", "a",
        "-m5",
        "-md64m",
        "-rr5p",
        "-ep1",
        f"-v{SPLIT_SIZE}",
        output_rar_base,
        input_target
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()
    return proc.returncode == 0

async def download_stream_url(url: str, dest_path: str, status_msg: Message) -> bool:
    state = {"last_update": 0}
    timeout = aiohttp.ClientTimeout(total=7200)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                return False
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(2 * 1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        await progress_callback(downloaded, total_size, status_msg, "Downloading Link", state)
            return True

@app.on_message(filters.command(["start", "help"]))
async def start_handler(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        await message.reply("Access denied.")
        return
    await message.reply(
        "Direct link or file sender active.\n"
        "Send any file or URL to compress into RAR with Best mode (-m5)."
    )

@app.on_message(filters.document | filters.video | filters.audio)
async def file_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply("Access denied.")
        return

    work_dir = os.path.join("workspace", str(uuid.uuid4()))
    os.makedirs(work_dir, exist_ok=True)
    status_msg = await message.reply("Task queued...")

    async with task_semaphore:
        try:
            await status_msg.edit_text("Downloading file from Telegram...")
            state = {"last_update": 0}

            file_path = await message.download(
                file_name=os.path.join(work_dir, "input_file"),
                progress=progress_callback,
                progress_args=(status_msg, "Downloading Telegram File", state)
            )

            original_name = message.document.file_name if message.document else "archive"
            base_name = os.path.splitext(original_name)[0]
            extract_dir = os.path.join(work_dir, "extracted")

            extracted = await extract_archive_if_needed(file_path, extract_dir)
            target = extract_dir if extracted else file_path

            rar_base = os.path.join(work_dir, base_name)
            await status_msg.edit_text("Compressing to RAR (Best -m5, 64MB Dict, 5% RR)...")
            success = await run_rar_compression(target, rar_base)

            if not success:
                await status_msg.edit_text("Compression failed.")
                return

            generated_parts = sorted([
                os.path.join(work_dir, f) for f in os.listdir(work_dir)
                if f.startswith(base_name) and (f.endswith(".rar") or ".part" in f)
            ])

            if not generated_parts:
                await status_msg.edit_text("No RAR outputs found.")
                return

            for idx, part in enumerate(generated_parts, 1):
                part_state = {"last_update": 0}
                await status_msg.edit_text(f"Uploading part {idx}/{len(generated_parts)}...")
                await message.reply_document(
                    document=part,
                    caption=f"`{os.path.basename(part)}`",
                    progress=progress_callback,
                    progress_args=(status_msg, f"Uploading Part {idx}/{len(generated_parts)}", part_state)
                )

            await status_msg.delete()

        except Exception as err:
            logger.exception("Processing error:")
            await status_msg.edit_text(f"Error: {str(err)}")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

@app.on_message(filters.regex(r"https?://[^\s]+"))
async def link_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply("Access denied.")
        return

    url = message.text.strip()
    work_dir = os.path.join("workspace", str(uuid.uuid4()))
    os.makedirs(work_dir, exist_ok=True)
    status_msg = await message.reply("Task queued...")

    async with task_semaphore:
        try:
            parsed = urlparse(url)
            raw_filename = unquote(os.path.basename(parsed.path)) or "downloaded_file"
            file_path = os.path.join(work_dir, raw_filename)

            await status_msg.edit_text("Downloading from link...")
            download_ok = await download_stream_url(url, file_path, status_msg)
            if not download_ok or not os.path.exists(file_path):
                await status_msg.edit_text("Download from link failed.")
                return

            base_name = os.path.splitext(raw_filename)[0]
            extract_dir = os.path.join(work_dir, "extracted")

            extracted = await extract_archive_if_needed(file_path, extract_dir)
            target = extract_dir if extracted else file_path

            rar_base = os.path.join(work_dir, base_name)
            await status_msg.edit_text("Compressing to RAR (Best -m5, 64MB Dict, 5% RR)...")
            success = await run_rar_compression(target, rar_base)

            if not success:
                await status_msg.edit_text("Compression failed.")
                return

            generated_parts = sorted([
                os.path.join(work_dir, f) for f in os.listdir(work_dir)
                if f.startswith(base_name) and (f.endswith(".rar") or ".part" in f)
            ])

            if not generated_parts:
                await status_msg.edit_text("No RAR outputs found.")
                return

            for idx, part in enumerate(generated_parts, 1):
                part_state = {"last_update": 0}
                await status_msg.edit_text(f"Uploading part {idx}/{len(generated_parts)}...")
                await message.reply_document(
                    document=part,
                    caption=f"`{os.path.basename(part)}`",
                    progress=progress_callback,
                    progress_args=(status_msg, f"Uploading Part {idx}/{len(generated_parts)}", part_state)
                )

            await status_msg.delete()

        except Exception as err:
            logger.exception("Processing error:")
            await status_msg.edit_text(f"Error: {str(err)}")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

async def start_web_health():
    app_web = web.Application()
    async def handler(request):
        return web.Response(text="Bot is running.")
    app_web.router.add_get("/", handler)
    app_web.router.add_get("/health", handler)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health server listening on port {PORT}")

async def main():
    await start_web_health()
    await app.start()
    logger.info("Bot started successfully.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        loop.run_until_complete(app.stop())
