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
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
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
ACTIVE_TASKS = {}

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

def format_time(seconds: float) -> str:
    if seconds is None or seconds < 0 or math.isinf(seconds) or math.isnan(seconds):
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return cleaned if cleaned else "archive"

def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS

def get_cancel_markup(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Cancel", callback_data=f"cancel:{task_id}")]
    ])

async def progress_callback(current, total, status_msg: Message, action_name: str, state: dict, task_id: str):
    task_info = ACTIVE_TASKS.get(task_id)
    if task_info and task_info.get("cancelled"):
        raise asyncio.CancelledError("Task was cancelled by user.")

    now = time.time()
    if now - state.get("last_update", 0) < 4 and current != total:
        return

    start_time = state.setdefault("start_time", now)
    state["last_update"] = now
    elapsed = max(0.1, now - start_time)
    speed = current / elapsed
    eta = (total - current) / speed if speed > 0 and total > current else 0

    percent = (current / total) * 100 if total > 0 else 0
    filled = int(percent // 10)
    bar = "█" * filled + "░" * (10 - filled)
    text = (
        f"**Status:** {action_name}\n"
        f"[{bar}] {percent:.1f}%\n"
        f"Transferred: {human_size(current)} / {human_size(total)}\n"
        f"Speed: {human_size(int(speed))}/s | ETA: {format_time(eta)}"
    )
    try:
        await status_msg.edit_text(text, reply_markup=get_cancel_markup(task_id))
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass

async def update_compression_progress(percent: int, status_msg: Message, state: dict, task_id: str):
    task_info = ACTIVE_TASKS.get(task_id)
    if task_info and task_info.get("cancelled"):
        raise asyncio.CancelledError("Task was cancelled by user.")

    now = time.time()
    if now - state.get("last_update", 0) < 4 and percent < 100:
        return

    start_time = state.setdefault("start_time", now)
    state["last_update"] = now
    elapsed = max(0.1, now - start_time)

    if percent > 0:
        total_estimated = elapsed / (percent / 100.0)
        eta = max(0.0, total_estimated - elapsed)
        eta_str = format_time(eta)
    else:
        eta_str = "Calculating..."

    filled = int(percent // 10)
    bar = "█" * filled + "░" * (10 - filled)
    text = (
        f"**Status:** Compressing to RAR5 Solid (-m5)\n"
        f"[{bar}] {percent}%\n"
        f"Mode: Best | Solid: ON | Dict: 64MB\n"
        f"Elapsed: {format_time(elapsed)} | ETA: {eta_str}"
    )
    try:
        await status_msg.edit_text(text, reply_markup=get_cancel_markup(task_id))
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass

async def send_detailed_error(message: Message, status_msg: Message, stage: str, exc: Exception, extra_log: str = ""):
    tb = traceback.format_exc()
    error_type = type(exc).__name__
    error_msg = str(exc)

    logger.error(f"Error [{stage}]: {error_type}: {error_msg}\n{tb}\nExtra: {extra_log}")

    content = (
        f"**Failed:** Task Execution Error\n"
        f"• **Stage:** `{stage}`\n"
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
            await status_msg.edit_text(f"Error in stage `{stage}`: {error_type} - {error_msg}\nFull log attached.")
            full_log = f"Stage: {stage}\nError: {error_type} - {error_msg}\n\nProcess Log:\n{extra_log}\n\nTraceback:\n{tb}"
            file_data = BytesIO(full_log.encode("utf-8"))
            file_data.name = "error_report.txt"
            await message.reply_document(document=file_data, caption=f"Error Log: {stage}")
    except Exception as err:
        logger.error(f"Failed to deliver error report: {err}")

async def try_extract_archive(file_path: str, extract_to: str, task_id: str) -> tuple[bool, str]:
    test_proc = await asyncio.create_subprocess_exec(
        "7z", "t", file_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    if task_id in ACTIVE_TASKS:
        ACTIVE_TASKS[task_id]["proc"] = test_proc

    stdout, stderr = await test_proc.communicate()
    if test_proc.returncode != 0:
        return False, "File is not an archive or format not supported."

    os.makedirs(extract_to, exist_ok=True)
    extract_proc = await asyncio.create_subprocess_exec(
        "7z", "x", file_path, f"-o{extract_to}", "-y",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    if task_id in ACTIVE_TASKS:
        ACTIVE_TASKS[task_id]["proc"] = extract_proc

    ext_out, ext_err = await extract_proc.communicate()
    out_log = ext_out.decode(errors="ignore") + "\n" + ext_err.decode(errors="ignore")
    return (extract_proc.returncode == 0), out_log

async def run_rar_compression(input_target: str, output_rar_archive: str, status_msg: Message, task_id: str) -> tuple[bool, str]:
    cmd = [
        "rar", "a",
        "-ma5",
        "-m5",
        "-s",
        "-md64m",
        f"-v{SPLIT_SIZE}"
    ]

    if os.path.isdir(input_target):
        working_dir = input_target
        cmd.append("-r")
        cmd.append(output_rar_archive)
        entries = os.listdir(input_target)
        if not entries:
            return False, "Extraction directory is empty."
        cmd.extend(entries)
    else:
        working_dir = os.path.dirname(input_target)
        cmd.append(output_rar_archive)
        cmd.append(os.path.basename(input_target))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=working_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    if task_id in ACTIVE_TASKS:
        ACTIVE_TASKS[task_id]["proc"] = proc

    full_log = []
    buf = ""
    state = {"start_time": time.time(), "last_update": 0}

    while True:
        task_info = ACTIVE_TASKS.get(task_id)
        if task_info and task_info.get("cancelled"):
            proc.kill()
            raise asyncio.CancelledError("Task was cancelled by user.")

        chunk = await proc.stdout.read(64)
        if not chunk:
            break
        text = chunk.decode(errors="ignore")
        full_log.append(text)
        buf += text

        matches = re.findall(r"(\d{1,3})%", buf)
        if matches:
            percent = int(matches[-1])
            if 0 <= percent <= 100:
                await update_compression_progress(percent, status_msg, state, task_id)

        if len(buf) > 256:
            buf = buf[-64:]

    _, stderr_data = await proc.communicate()
    full_log.append(stderr_data.decode(errors="ignore"))
    return (proc.returncode == 0), "".join(full_log)

async def run_rar_test(rar_file: str, task_id: str) -> tuple[bool, str]:
    cmd = ["rar", "t", "-y", rar_file]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    if task_id in ACTIVE_TASKS:
        ACTIVE_TASKS[task_id]["proc"] = proc

    stdout, stderr = await proc.communicate()
    log = stdout.decode(errors="ignore") + "\n" + stderr.decode(errors="ignore")
    return (proc.returncode == 0), log

async def get_gofile_token(session: aiohttp.ClientSession) -> str:
    try:
        async with session.post("https://api.gofile.io/accounts") as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("status") == "ok":
                    return data["data"]["token"]
    except Exception as e:
        logger.warning(f"Failed to obtain Gofile guest token: {e}")
    return ""

async def resolve_gofile_url(url: str, session: aiohttp.ClientSession) -> tuple[str, str]:
    token = await get_gofile_token(session)
    if not token:
        return url, ""

    match = re.search(r"(?:gofile\.io/(?:d/|download/web/)|contents/)([a-zA-Z0-9-]+)", url)
    if not match:
        return url, token

    content_id = match.group(1)
    api_url = f"https://api.gofile.io/contents/{content_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Authorization": f"Bearer {token}"
    }

    try:
        async with session.get(api_url, headers=headers) as resp:
            if resp.status == 200:
                res_data = await resp.json()
                if res_data.get("status") == "ok":
                    d = res_data.get("data", {})
                    if d.get("type") == "file" and d.get("link"):
                        return d["link"], token
                    elif d.get("type") == "folder":
                        children = d.get("children", {})
                        for item in children.values():
                            if item.get("link"):
                                return item["link"], token
    except Exception as e:
        logger.warning(f"Gofile API resolution failed: {e}")

    return url, token

async def download_stream_url(url: str, dest_dir: str, status_msg: Message, task_id: str) -> tuple[bool, str, str]:
    state = {"start_time": time.time(), "last_update": 0}
    timeout = aiohttp.ClientTimeout(total=7200)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Referer": "https://gofile.io/",
    }
    cookies = {}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        target_url = url
        if "gofile.io" in url:
            target_url, gofile_token = await resolve_gofile_url(url, session)
            if gofile_token:
                headers["Authorization"] = f"Bearer {gofile_token}"
                cookies["accountToken"] = gofile_token

        async with session.get(target_url, headers=headers, cookies=cookies, allow_redirects=True) as resp:
            if resp.status != 200:
                return False, "", f"HTTP Status {resp.status}: {resp.reason}"

            content_type = resp.headers.get("Content-Type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                preview = (await resp.content.read(2048)).decode(errors="ignore")
                return False, "", f"Server returned {content_type} instead of binary data. Content: {preview[:300]}"

            final_name = ""
            cd = resp.headers.get("Content-Disposition", "")
            if cd:
                cd_matches = re.findall(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)["\']?', cd, flags=re.IGNORECASE)
                if cd_matches:
                    final_name = unquote(cd_matches[-1].strip())

            if not final_name:
                parsed = urlparse(str(resp.url))
                final_name = unquote(os.path.basename(parsed.path)) or "downloaded_file"

            final_name = sanitize_filename(final_name)
            dest_path = os.path.join(dest_dir, final_name)

            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0

            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(2 * 1024 * 1024):
                    task_info = ACTIVE_TASKS.get(task_id)
                    if task_info and task_info.get("cancelled"):
                        raise asyncio.CancelledError("Task was cancelled by user.")
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        await progress_callback(downloaded, total_size, status_msg, "Downloading Link", state, task_id)

            if os.path.getsize(dest_path) < 10240 and total_size == 0:
                with open(dest_path, "r", errors="ignore") as f:
                    data_preview = f.read(512)
                if "<html" in data_preview.lower() or '{"status":' in data_preview:
                    return False, dest_path, f"Remote server provided an error page: {data_preview[:200]}"

            return True, dest_path, ""

def find_rar_outputs(search_dir: str) -> list[str]:
    rar_files = []
    for root, _, files in os.walk(search_dir):
        for f in files:
            if f.lower().endswith(".rar") or ".part" in f.lower():
                rar_files.append(os.path.join(root, f))
    rar_files.sort()
    return rar_files

async def process_task(task_id: str, should_test: bool):
    task_data = ACTIVE_TASKS.get(task_id)
    if not task_data:
        return

    message = task_data["message"]
    status_msg = task_data["status_msg"]
    mode = task_data["mode"]
    url = task_data.get("url")

    work_dir = os.path.join("/tmp", f"rar_{uuid.uuid4().hex}")
    output_dir = os.path.join(work_dir, "output")
    task_data["work_dir"] = work_dir
    os.makedirs(output_dir, exist_ok=True)
    stage = "Queue"
    extra_log = ""

    async with task_semaphore:
        try:
            if task_data.get("cancelled"):
                raise asyncio.CancelledError()

            if mode == "file":
                stage = "Downloading from Telegram"
                await status_msg.edit_text(f"Status: {stage}...", reply_markup=get_cancel_markup(task_id))
                state = {"start_time": time.time(), "last_update": 0}

                file_attr = message.document or message.video or message.audio
                orig_name = getattr(file_attr, "file_name", None) or "file"
                orig_name = sanitize_filename(orig_name)
                save_dest = os.path.join(work_dir, orig_name)

                file_path = await message.download(
                    file_name=save_dest,
                    progress=progress_callback,
                    progress_args=(status_msg, stage, state, task_id)
                )
                base_name = sanitize_filename(os.path.splitext(orig_name)[0])
            else:
                stage = "Downloading URL"
                await status_msg.edit_text(f"Status: {stage}...", reply_markup=get_cancel_markup(task_id))
                download_ok, file_path, dl_err = await download_stream_url(url, work_dir, status_msg, task_id)
                if not download_ok:
                    raise RuntimeError(f"URL download failed: {dl_err}")
                base_name = sanitize_filename(os.path.splitext(os.path.basename(file_path))[0])

            if task_data.get("cancelled"):
                raise asyncio.CancelledError()

            extract_dir = os.path.join(work_dir, "extracted")
            stage = "Analyzing and Extracting Archive"
            await status_msg.edit_text(f"Status: {stage}...", reply_markup=get_cancel_markup(task_id))
            extracted, ext_log = await try_extract_archive(file_path, extract_dir, task_id)
            extra_log += f"\n--- Extraction Log ---\n{ext_log}"

            target = extract_dir if extracted else file_path

            if task_data.get("cancelled"):
                raise asyncio.CancelledError()

            stage = "Compressing to RAR5 Solid (-m5)"
            rar_target = os.path.join(output_dir, f"{base_name}.rar")
            await status_msg.edit_text(f"Status: {stage} (0%)...", reply_markup=get_cancel_markup(task_id))
            success, rar_log = await run_rar_compression(target, rar_target, status_msg, task_id)
            extra_log += f"\n--- RAR Log ---\n{rar_log}"

            if not success:
                raise RuntimeError(f"RAR execution failed:\n{rar_log}")

            if task_data.get("cancelled"):
                raise asyncio.CancelledError()

            stage = "Scanning for RAR Files"
            generated_parts = find_rar_outputs(output_dir)
            if not generated_parts:
                dir_contents = os.listdir(output_dir)
                raise FileNotFoundError(f"No RAR files found in output.\nDirectory contents: {dir_contents}")

            if should_test:
                stage = "Testing Archive Integrity (rar t)"
                await status_msg.edit_text("Status: Running RAR integrity test (`rar t`)...", reply_markup=get_cancel_markup(task_id))
                test_passed, test_log = await run_rar_test(generated_parts[0], task_id)
                extra_log += f"\n--- Test Log ---\n{test_log}"
                if not test_passed:
                    raise RuntimeError(f"RAR Integrity Test Failed:\n{test_log}")
                await status_msg.edit_text("Status: RAR Integrity Test Passed (100% OK). Preparing upload...", reply_markup=get_cancel_markup(task_id))
                await asyncio.sleep(1)

            if task_data.get("cancelled"):
                raise asyncio.CancelledError()

            stage = "Uploading Part(s)"
            for idx, part in enumerate(generated_parts, 1):
                if task_data.get("cancelled"):
                    raise asyncio.CancelledError()

                part_state = {"start_time": time.time(), "last_update": 0}
                await status_msg.edit_text(f"Uploading part {idx}/{len(generated_parts)}...", reply_markup=get_cancel_markup(task_id))
                await message.reply_document(
                    document=part,
                    caption=f"`{os.path.basename(part)}`",
                    progress=progress_callback,
                    progress_args=(status_msg, f"Uploading Part {idx}/{len(generated_parts)}", part_state, task_id)
                )

            await status_msg.delete()

        except asyncio.CancelledError:
            logger.info(f"Task {task_id} successfully cancelled.")
            try:
                await status_msg.edit_text("Task was cancelled by user.")
            except Exception:
                pass
        except Exception as err:
            await send_detailed_error(message, status_msg, stage, err, extra_log)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            ACTIVE_TASKS.pop(task_id, None)

@app.on_message(filters.command(["start", "help"]))
async def start_handler(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        await message.reply("Access denied.")
        return
    await message.reply("Send any file or direct download link to compress into RAR (Best -m5).")

@app.on_message(filters.document | filters.video | filters.audio)
async def file_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply("Access denied.")
        return

    task_id = uuid.uuid4().hex[:8]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes (Test with rar t)", callback_data=f"test:yes:{task_id}"),
            InlineKeyboardButton("No (Skip test)", callback_data=f"test:no:{task_id}")
        ],
        [
            InlineKeyboardButton("Cancel", callback_data=f"cancel:{task_id}")
        ]
    ])
    prompt = await message.reply(
        "Do you want to run an archive integrity test (`rar t`) after compression finishes?",
        reply_markup=keyboard
    )
    ACTIVE_TASKS[task_id] = {
        "mode": "file",
        "message": message,
        "status_msg": prompt,
        "user_id": user_id,
        "cancelled": False,
        "proc": None,
        "async_task": None
    }

@app.on_message(filters.regex(r"https?://[^\s]+"))
async def link_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply("Access denied.")
        return

    task_id = uuid.uuid4().hex[:8]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes (Test with rar t)", callback_data=f"test:yes:{task_id}"),
            InlineKeyboardButton("No (Skip test)", callback_data=f"test:no:{task_id}")
        ],
        [
            InlineKeyboardButton("Cancel", callback_data=f"cancel:{task_id}")
        ]
    ])
    prompt = await message.reply(
        "Do you want to run an archive integrity test (`rar t`) after compression finishes?",
        reply_markup=keyboard
    )
    ACTIVE_TASKS[task_id] = {
        "mode": "url",
        "url": message.text.strip(),
        "message": message,
        "status_msg": prompt,
        "user_id": user_id,
        "cancelled": False,
        "proc": None,
        "async_task": None
    }

@app.on_callback_query(filters.regex(r"^cancel:([a-f0-9]+)$"))
async def cancel_callback_handler(client: Client, callback_query: CallbackQuery):
    task_id = callback_query.data.split(":")[1]
    task_data = ACTIVE_TASKS.get(task_id)

    if not task_data:
        await callback_query.answer("Task expired or already finished.", show_alert=True)
        return

    if callback_query.from_user.id != task_data["user_id"]:
        await callback_query.answer("Unauthorized.", show_alert=True)
        return

    await callback_query.answer("Cancelling task...")
    task_data["cancelled"] = True

    proc = task_data.get("proc")
    if proc:
        try:
            proc.kill()
        except Exception:
            pass

    async_task = task_data.get("async_task")
    if async_task and not async_task.done():
        async_task.cancel()

    work_dir = task_data.get("work_dir")
    if work_dir and os.path.exists(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)

    try:
        await task_data["status_msg"].edit_text("Task was cancelled by user.")
    except Exception:
        pass

    ACTIVE_TASKS.pop(task_id, None)

@app.on_callback_query(filters.regex(r"^test:(yes|no):([a-f0-9]+)$"))
async def test_callback_handler(client: Client, callback_query: CallbackQuery):
    action, task_id = callback_query.data.split(":")[1], callback_query.data.split(":")[2]
    task_data = ACTIVE_TASKS.get(task_id)

    if not task_data:
        await callback_query.answer("Task expired or not found.", show_alert=True)
        return

    if callback_query.from_user.id != task_data["user_id"]:
        await callback_query.answer("Unauthorized.", show_alert=True)
        return

    await callback_query.answer()
    should_test = (action == "yes")
    await task_data["status_msg"].edit_text("Task queued...", reply_markup=get_cancel_markup(task_id))

    t = asyncio.create_task(process_task(task_id, should_test))
    task_data["async_task"] = t

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
