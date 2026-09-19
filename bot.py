import os
import re
import sys
import time
import math
import uuid
import shutil
import asyncio
import logging
import traceback
from io import BytesIO
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

raw_api_id = os.getenv("API_ID", "").strip()
if not raw_api_id.isdigit():
    logger.critical(f"FATAL: API_ID must be numeric, received: '{raw_api_id}'")
    sys.exit(1)

API_ID = int(raw_api_id)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
SPLIT_SIZE = os.getenv("SPLIT_SIZE", "1950m")
PORT = int(os.getenv("PORT", "8080"))

ALLOWED_USERS = set()
if ALLOWED_USERS_RAW:
    for uid in ALLOWED_USERS_RAW.split(","):
        uid_clean = uid.strip()
        if uid_clean.isdigit():
            ALLOWED_USERS.add(int(uid_clean))

if not API_HASH or not BOT_TOKEN:
    logger.critical("FATAL: API_HASH and BOT_TOKEN are required.")
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

def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return cleaned if cleaned else "archive"

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

async def send_detailed_error(message: Message, status_msg: Message, stage: str, exc: Exception, extra_log: str = ""):
    tb = traceback.format_exc()
    error_type = type(exc).__name__
    error_msg = str(exc)

    logger.error(f"Error [{stage}]: {error_type}: {error_msg}\n{tb}\nExtra: {extra_log}")

    content = (
        f"❌ **Stage:** `{stage}`\n"
        f"• **Type:** `{error_type}`\n"
        f"• **Detail:** `{error_msg}`\n"
    )
    if extra_log:
        content += f"\n**Process Output:**\n```{extra_log[-1500:]}```\n"

    content += f"\n**Traceback:**\n```{tb[-1500:]}```"

    try:
        if len(content) <= 4000:
            await status_msg.edit_text(content)
        else:
            await status_msg.edit_text(f"❌ Error in stage `{stage}`: {error_type} - {error_msg}\nFull log attached.")
            full_log = f"Stage: {stage}\nError: {error_type} - {error_msg}\n\nExtra Log:\n{extra_log}\n\nTraceback:\n{tb}"
            file_data = BytesIO(full_log.encode("utf-8"))
            file_data.name = "error_report.txt"
            await message.reply_document(document=file_data, caption=f"Error Log: {stage}")
    except Exception as err:
        logger.error(f"Failed to send error to telegram: {err}")

async def extract_archive_if_needed(file_path: str, extract_to: str) -> tuple[bool, str]:
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
        stdout, stderr = await proc.communicate()
        out_log = stdout.decode(errors="ignore") + "\n" + stderr.decode(errors="ignore")
        return (proc.returncode == 0), out_log
    return False, ""

async def run_rar_compression(input_target: str, output_rar_archive: str) -> tuple[bool, str]:
    cmd = [
        "rar", "a",
        "-m5",
        "-md64m",
        "-rr5p",
        "-ep1",
        f"-v{SPLIT_SIZE}"
    ]
    if os.path.isdir(input_target):
        cmd.append("-r")

    cmd.extend([output_rar_archive, input_target])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    out_log = stdout.decode(errors="ignore") + "\n" + stderr.decode(errors="ignore")
    return (proc.returncode == 0), out_log

async def download_stream_url(url: str, dest_path: str, status_msg: Message) -> tuple[bool, str]:
    state = {"last_update": 0}
    timeout = aiohttp.ClientTimeout(total=7200)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                return False, f"HTTP Status {resp.status}: {resp.reason}"
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(2 * 1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        await progress_callback(downloaded, total_size, status_msg, "Downloading Link", state)
            return True, ""

def find_rar_outputs(work_dir: str) -> list[str]:
    files = os.listdir(work_dir)
    rar_files = [
        os.path.join(work_dir, f) for f in files
        if f.lower().endswith(".rar") or ".part" in f.lower()
    ]
    rar_files.sort()
    return rar_files

@app.on_message(filters.command(["start", "help"]))
async def start_handler(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        await message.reply("Access denied.")
        return
    await message.reply("Send any file or URL to compress into RAR (Best -m5).")

@app.on_message(filters.document | filters.video | filters.audio)
async def file_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply("Access denied.")
        return

    work_dir = os.path.join("/tmp", f"rar_{uuid.uuid4().hex}")
    os.makedirs(work_dir, exist_ok=True)
    status_msg = await message.reply("Task queued...")
    stage = "Queue"
    extra_log = ""

    async with task_semaphore:
        try:
            stage = "Downloading from Telegram"
            await status_msg.edit_text(f"Status: {stage}...")
            state = {"last_update": 0}

            file_attr = message.document or message.video or message.audio
            orig_name = getattr(file_attr, "file_name", None) or "input_file"
            orig_name = sanitize_filename(orig_name)
            save_dest = os.path.join(work_dir, orig_name)

            file_path = await message.download(
                file_name=save_dest,
                progress=progress_callback,
                progress_args=(status_msg, stage, state)
            )

            base_name = sanitize_filename(os.path.splitext(orig_name)[0])
            extract_dir = os.path.join(work_dir, f"{base_name}_extracted")

            stage = "Extracting Archive"
            extracted, ext_log = await extract_archive_if_needed(file_path, extract_dir)
            extra_log += f"\n--- Extraction Log ---\n{ext_log}"

            target = extract_dir if extracted else file_path

            stage = "Compressing to RAR (-m5)"
            rar_target = os.path.join(work_dir, f"{base_name}.rar")
            await status_msg.edit_text(f"Status: {stage}...")
            success, rar_log = await run_rar_compression(target, rar_target)
            extra_log += f"\n--- RAR Log ---\n{rar_log}"

            if not success:
                raise RuntimeError(f"RAR exited with error status.\n{rar_log}")

            stage = "Scanning for RAR Files"
            generated_parts = find_rar_outputs(work_dir)

            if not generated_parts:
                dir_contents = os.listdir(work_dir)
                raise FileNotFoundError(
                    f"No RAR files found after compression.\n"
                    f"Directory contents: {dir_contents}"
                )

            stage = "Uploading Part(s)"
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
            await send_detailed_error(message, status_msg, stage, err, extra_log)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

@app.on_message(filters.regex(r"https?://[^\s]+"))
async def link_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply("Access denied.")
        return

    url = message.text.strip()
    work_dir = os.path.join("/tmp", f"rar_{uuid.uuid4().hex}")
    os.makedirs(work_dir, exist_ok=True)
    status_msg = await message.reply("Task queued...")
    stage = "Queue"
    extra_log = ""

    async with task_semaphore:
        try:
            stage = "Downloading URL"
            parsed = urlparse(url)
            raw_filename = unquote(os.path.basename(parsed.path)) or "downloaded_file"
            raw_filename = sanitize_filename(raw_filename)
            file_path = os.path.join(work_dir, raw_filename)

            await status_msg.edit_text(f"Status: {stage}...")
            download_ok, dl_err = await download_stream_url(url, file_path, status_msg)
            if not download_ok:
                raise RuntimeError(f"URL download failed: {dl_err}")

            base_name = sanitize_filename(os.path.splitext(raw_filename)[0])
            extract_dir = os.path.join(work_dir, f"{base_name}_extracted")

            stage = "Extracting Archive"
            extracted, ext_log = await extract_archive_if_needed(file_path, extract_dir)
            extra_log += f"\n--- Extraction Log ---\n{ext_log}"

            target = extract_dir if extracted else file_path

            stage = "Compressing to RAR (-m5)"
            rar_target = os.path.join(work_dir, f"{base_name}.rar")
            await status_msg.edit_text(f"Status: {stage}...")
            success, rar_log = await run_rar_compression(target, rar_target)
            extra_log += f"\n--- RAR Log ---\n{rar_log}"

            if not success:
                raise RuntimeError(f"RAR exited with error status.\n{rar_log}")

            stage = "Scanning for RAR Files"
            generated_parts = find_rar_outputs(work_dir)

            if not generated_parts:
                dir_contents = os.listdir(work_dir)
                raise FileNotFoundError(
                    f"No RAR files found after compression.\n"
                    f"Directory contents: {dir_contents}"
                )

            stage = "Uploading Part(s)"
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
            await send_detailed_error(message, status_msg, stage, err, extra_log)
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
