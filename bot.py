import os
import re
import gc
import sys
import json
import time
import math
import uuid
import shutil
import signal
import hashlib
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
from pyrogram.errors import FloodWait, MessageNotModified

# ==================== LOGGING & CONFIG ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("RARBotPro")

raw_api_id = os.getenv("API_ID", "").strip()
if not raw_api_id.isdigit():
    logger.critical(f"FATAL: API_ID must be numeric, received: '{raw_api_id}'")
    sys.exit(1)

API_ID = int(raw_api_id)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
DEFAULT_SPLIT_SIZE = os.getenv("SPLIT_SIZE", "1950m")
PORT = int(os.getenv("PORT", "8080"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT", "1"))
WORK_BASE_DIR = os.getenv("WORK_DIR", "/tmp")
DATA_DIR = os.getenv("DATA_DIR", "./bot_data")

os.makedirs(DATA_DIR, exist_ok=True)
PREFS_FILE = os.path.join(DATA_DIR, "user_prefs.json")

ALLOWED_USERS = set()
if ALLOWED_USERS_RAW:
    for uid in ALLOWED_USERS_RAW.split(","):
        uid_clean = uid.strip()
        if uid_clean.isdigit():
            ALLOWED_USERS.add(int(uid_clean))

if not API_HASH or not BOT_TOKEN:
    logger.critical("FATAL: API_HASH and BOT_TOKEN are required.")
    sys.exit(1)

# Extensions that look like archives (ZIP-based) but MUST NOT be extracted in Auto mode
NO_EXTRACT_EXTENSIONS = {
    ".apk", ".xapk", ".apks", ".apkm", ".aab", ".jar", ".war", ".ear",
    ".ipa", ".msix", ".appx", ".deb", ".rpm", ".whl", ".egg",
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp",
    ".epub", ".cbz", ".cbr", ".ora", ".kra", ".xp3"
}

COMPRESSION_LEVELS = {
    0: "Store (-m0)",
    1: "Fastest (-m1)",
    2: "Fast (-m2)",
    3: "Normal (-m3)",
    4: "Good (-m4)",
    5: "Best (-m5)"
}

SPLIT_PRESETS = ["0", "500m", "1000m", "1950m", "3900m"]
DICT_PRESETS = ["32m", "64m", "128m"]
EXTRACT_MODES = ["auto", "never", "force"]

BOT_START_TIME = time.time()
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
ACTIVE_TASKS = {}
AWAITING_INPUT = {}  # user_id -> {"task_id": str, "type": "rename" | "password" | "split"}
USER_PREFS = {}

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive"
}

app = Client(
    "rar_worker_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=DATA_DIR
)

# ==================== PERSISTENCE & PREFS ====================
def load_prefs():
    global USER_PREFS
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r", encoding="utf-8") as f:
                USER_PREFS = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load user prefs: {e}")
            USER_PREFS = {}

def save_prefs():
    try:
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(USER_PREFS, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save user prefs: {e}")

def get_user_prefs(user_id: int) -> dict:
    uid = str(user_id)
    if uid not in USER_PREFS:
        USER_PREFS[uid] = {
            "level": 5,
            "split": DEFAULT_SPLIT_SIZE,
            "dict_size": "64m",
            "solid": True,
            "recovery": False,
            "encrypt_headers": True,
            "extract_mode": "auto",
            "checksum": True,
            "default_password": None
        }
        save_prefs()
    return USER_PREFS[uid]

def get_user_thumb(user_id: int) -> str | None:
    path = os.path.join(DATA_DIR, f"thumb_{user_id}.jpg")
    return path if os.path.exists(path) else None

def cleanup_orphaned_dirs():
    removed = 0
    try:
        for item in os.listdir(WORK_BASE_DIR):
            if item.startswith("rar_"):
                full_path = os.path.join(WORK_BASE_DIR, item)
                if os.path.isdir(full_path):
                    shutil.rmtree(full_path, ignore_errors=True)
                    removed += 1
    except Exception as e:
        logger.warning(f"Startup cleanup error: {e}")
    return removed

# ==================== HELPER FUNCTIONS ====================
def human_size(size_bytes: int | float) -> str:
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    i = min(i, len(units) - 1)
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

def sanitize_filename(name: str, max_len: int = 80) -> str:
    if not name:
        return "archive"
    name = re.sub(r'[\r\n\t]+', ' ', name)
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    if len(cleaned.encode('utf-8')) > max_len:
        while len(cleaned.encode('utf-8')) > max_len and len(cleaned) > 1:
            cleaned = cleaned[:-1]
        cleaned = cleaned.rstrip(' ._-')
    return cleaned if cleaned else "archive"

def strip_known_archive_ext(filename: str) -> str:
    lower = filename.lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".part1.rar", ".part01.rar"):
        if lower.endswith(compound):
            return filename[:-len(compound)]
    return os.path.splitext(filename)[0]

def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS

def should_extract_file(file_path: str, extract_mode: str) -> bool:
    if extract_mode == "never":
        return False
    if extract_mode == "force":
        return True
    # Auto mode: check against package/office extensions
    ext = os.path.splitext(file_path)[1].lower()
    if ext in NO_EXTRACT_EXTENSIONS:
        return False
    return True

def get_system_stats() -> dict:
    disk = shutil.disk_usage(WORK_BASE_DIR)
    mem_total, mem_avail = 0, 0
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) * 1024
    except Exception:
        pass

    load_avg = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    uptime = time.time() - BOT_START_TIME
    return {
        "disk_free": disk.free,
        "disk_total": disk.total,
        "disk_percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0,
        "mem_total": mem_total,
        "mem_used": max(0, mem_total - mem_avail),
        "load": round(load_avg, 2),
        "uptime": format_time(uptime)
    }

def compute_hashes(file_path: str) -> tuple[str, str]:
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()

# ==================== URL & NETWORK ====================
def parse_link_message(text: str) -> tuple[str, str | None, dict]:
    """
    Supports formats:
    1) https://example.com/file.zip CustomName
    2) https://example.com/file.zip | CustomName | Cookie: a=b; Referer: https://...
    """
    custom_headers = {}
    custom_name = None

    if "|" in text:
        segments = [s.strip() for s in text.split("|")]
        url = segments[0]
        if len(segments) > 1 and segments[1]:
            custom_name = segments[1]
        if len(segments) > 2 and segments[2]:
            for hdr in segments[2].split(";"):
                if ":" in hdr:
                    k, v = hdr.split(":", 1)
                    custom_headers[k.strip()] = v.strip()
    else:
        parts = text.strip().split(maxsplit=1)
        url = parts[0]
        if len(parts) > 1:
            custom_name = parts[1].split("\n")[0].strip()

    if custom_name:
        custom_name = sanitize_filename(custom_name)
        if custom_name.lower().endswith(".rar"):
            custom_name = custom_name[:-4].strip()

    return url, custom_name, custom_headers

def extract_filename_from_headers(headers: dict, url: str) -> str:
    cd = headers.get("Content-Disposition", "")
    if cd:
        match_utf8 = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.IGNORECASE)
        if match_utf8:
            return unquote(match_utf8.group(1).strip('"\' '))
        match = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.IGNORECASE)
        if match:
            return unquote(match.group(1).strip())
        match_bare = re.search(r'filename\s*=\s*([^;]+)', cd, re.IGNORECASE)
        if match_bare:
            return unquote(match_bare.group(1).strip('"\' '))
    parsed = urlparse(url)
    raw_name = os.path.basename(parsed.path)
    return unquote(raw_name) if raw_name else "downloaded_file"

async def validate_url(url: str, extra_headers: dict = None) -> tuple[bool, str, str, int]:
    timeout = aiohttp.ClientTimeout(total=25)
    req_headers = {**HTTP_HEADERS, **(extra_headers or {})}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=req_headers) as session:
            try:
                async with session.head(url, allow_redirects=True) as resp:
                    if resp.status in (200, 206):
                        fname = extract_filename_from_headers(dict(resp.headers), str(resp.url))
                        size = int(resp.headers.get("Content-Length", 0))
                        return True, "", fname, size
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass

            async with session.get(url, allow_redirects=True) as resp:
                if resp.status in (200, 206):
                    fname = extract_filename_from_headers(dict(resp.headers), str(resp.url))
                    size = int(resp.headers.get("Content-Length", 0))
                    return True, "", fname, size
                elif resp.status == 410:
                    return False, "Link expired (410 Gone).", "", 0
                elif resp.status == 404:
                    return False, "Resource not found (404 Not Found).", "", 0
                elif resp.status == 403:
                    return False, "Access forbidden (403 Forbidden).", "", 0
                else:
                    return False, f"HTTP {resp.status}: {resp.reason}", "", 0

    except Exception as e:
        return False, f"Validation error: {type(e).__name__} - {e}", "", 0

# ==================== UI MARKUPS & TEXT ====================
def get_cancel_markup(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Cancel Task", callback_data=f"cancel:{task_id}")]
    ])

def get_setup_markup(task_id: str) -> InlineKeyboardMarkup:
    t = ACTIVE_TASKS.get(task_id, {})
    lvl = t.get("level", 5)
    split = t.get("split", DEFAULT_SPLIT_SIZE)
    split_label = "No Split" if split in ("0", "", None) else split.upper()
    ext_mode = t.get("extract_mode", "auto").capitalize()
    solid_label = "ON" if t.get("solid", True) else "OFF"
    rr_label = "3%" if t.get("recovery", False) else "OFF"
    dict_label = t.get("dict_size", "64m").upper()
    pwd_set = "Set" if t.get("password") else "None"
    hp_label = "Full (-hp)" if t.get("encrypt_headers", True) else "Data (-p)"

    rows = [
        [
            InlineKeyboardButton("Start + Test (rar t)", callback_data=f"start:yes:{task_id}"),
            InlineKeyboardButton("Start (Fast / No Test)", callback_data=f"start:no:{task_id}")
        ],
        [
            InlineKeyboardButton(f"Level: m{lvl}", callback_data=f"cfg:lvl:{task_id}"),
            InlineKeyboardButton(f"Split: {split_label}", callback_data=f"cfg:split:{task_id}"),
            InlineKeyboardButton(f"Extract: {ext_mode}", callback_data=f"cfg:ext:{task_id}")
        ],
        [
            InlineKeyboardButton(f"Solid: {solid_label}", callback_data=f"cfg:solid:{task_id}"),
            InlineKeyboardButton(f"Recov: {rr_label}", callback_data=f"cfg:rr:{task_id}"),
            InlineKeyboardButton(f"Dict: {dict_label}", callback_data=f"cfg:dict:{task_id}")
        ],
        [
            InlineKeyboardButton("Rename", callback_data=f"input:rename:{task_id}"),
            InlineKeyboardButton(f"Pass: {pwd_set}", callback_data=f"input:password:{task_id}"),
            InlineKeyboardButton(f"Enc: {hp_label}", callback_data=f"cfg:enc:{task_id}")
        ],
        [
            InlineKeyboardButton("Custom Split Size", callback_data=f"input:split:{task_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"cancel:{task_id}")
        ]
    ]
    return InlineKeyboardMarkup(rows)

def get_setup_text(task_data: dict) -> str:
    name = task_data.get("custom_name") or task_data.get("default_name")
    orig_file = task_data.get("orig_filename", "Unknown")
    ext_mode = task_data.get("extract_mode", "auto")
    will_extract = should_extract_file(orig_file, ext_mode)
    extract_status = f"`{ext_mode.upper()}` -> " + ("Will Extract First" if will_extract else "**Direct Compress (No Extract)**")

    size_val = task_data.get("remote_size", 0)
    size_str = f"`{human_size(size_val)}`" if size_val > 0 else "`Unknown`"
    pwd = task_data.get("password")
    pwd_str = f"`{'*' * len(pwd)}` ({'Headers+Data' if task_data.get('encrypt_headers', True) else 'Data Only'})" if pwd else "`None`"
    split_val = task_data.get("split", "0")
    split_str = "`Single Archive`" if split_val in ("0", "", None) else f"`{split_val}`"

    return (
        f"**Archive Setup Panel**\n\n"
        f"- **Source File:** `{orig_file}`\n"
        f"- **Target Output:** `{name}.rar`\n"
        f"- **Source Size:** {size_str}\n"
        f"- **Extraction Mode:** {extract_status}\n"
        f"- **Compression:** `{COMPRESSION_LEVELS.get(task_data.get('level', 5))}`\n"
        f"- **Split Size:** {split_str} | **Dict:** `{task_data.get('dict_size', '64m')}`\n"
        f"- **Solid:** `{'Yes' if task_data.get('solid') else 'No'}` | **Recovery:** `{'3%' if task_data.get('recovery') else 'Off'}`\n"
        f"- **Password:** {pwd_str}\n\n"
        f"Adjust options below or press **Start**:"
    )

# ==================== PROGRESS & ERROR HANDLERS ====================
def render_bar(percent: float) -> str:
    filled = max(0, min(10, int(percent // 10)))
    return "█" * filled + "░" * (10 - filled)

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
    bar = render_bar(percent)
    text = (
        f"**Status:** {action_name}\n"
        f"`[{bar}] {percent:.1f}%`\n"
        f"**Processed:** `{human_size(current)}` / `{human_size(total)}`\n"
        f"**Speed:** `{human_size(int(speed))}/s` | **ETA:** `{format_time(eta)}`"
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

    bar = render_bar(percent)
    lvl = task_info.get("level", 5) if task_info else 5
    text = (
        f"**Status:** Compressing to RAR5 (`-m{lvl}`)\n"
        f"`[{bar}] {percent}%`\n"
        f"**Solid:** `{'ON' if task_info.get('solid') else 'OFF'}` | **Dict:** `{task_info.get('dict_size', '64m').upper()}`\n"
        f"**Elapsed:** `{format_time(elapsed)}` | **ETA:** `{eta_str}`"
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
        f"**Task Execution Failed**\n"
        f"- **Stage:** `{stage}`\n"
        f"- **Type:** `{error_type}`\n"
        f"- **Detail:** `{error_msg}`\n"
    )
    if extra_log:
        content += f"\n**Process Output:**\n```{extra_log[-1200:]}```\n"
    content += f"\n**Traceback:**\n```{tb[-1200:]}```"

    try:
        if len(content) <= 4000:
            await status_msg.edit_text(content)
        else:
            await status_msg.edit_text(f"**Error in stage `{stage}`:** `{error_type} - {error_msg}`\nFull diagnostic log attached below.")
            full_log = f"Stage: {stage}\nError: {error_type} - {error_msg}\n\nProcess Log:\n{extra_log}\n\nTraceback:\n{tb}"
            file_data = BytesIO(full_log.encode("utf-8"))
            file_data.name = "error_report.txt"
            await message.reply_document(document=file_data, caption=f"Error Log: {stage}")
    except Exception as err:
        logger.error(f"Failed to deliver error report: {err}")

# ==================== CORE WORKER PROCESSES ====================
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
        return False, "File is not a standard archive or is password-protected."

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
    task_info = ACTIVE_TASKS.get(task_id, {})
    lvl = task_info.get("level", 5)
    dict_size = task_info.get("dict_size", "64m")
    split = task_info.get("split", DEFAULT_SPLIT_SIZE)
    solid = task_info.get("solid", True)
    recovery = task_info.get("recovery", False)
    password = task_info.get("password")
    enc_headers = task_info.get("encrypt_headers", True)

    cmd = [
        "rar", "a",
        "-ma5",
        f"-m{lvl}",
        f"-md{dict_size}",
        "-ep1",
        "-y"
    ]

    if solid and lvl > 0:
        cmd.append("-s")
    if recovery:
        cmd.append("-rr3p")
    if split and split not in ("0", "none", "off"):
        cmd.append(f"-v{split}")
    if password:
        if enc_headers:
            cmd.append(f"-hp{password}")
        else:
            cmd.append(f"-p{password}")

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
        if task_info.get("cancelled"):
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
    task_info = ACTIVE_TASKS.get(task_id, {})
    password = task_info.get("password")
    cmd = ["rar", "t", "-y"]
    if password:
        cmd.append(f"-p{password}")
    cmd.append(rar_file)

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

async def download_stream_url(url: str, dest_path: str, status_msg: Message, task_id: str, extra_headers: dict = None) -> tuple[bool, str]:
    state = {"start_time": time.time(), "last_update": 0}
    timeout = aiohttp.ClientTimeout(total=7200, sock_read=60)
    base_headers = {**HTTP_HEADERS, **(extra_headers or {})}

    max_retries = 3
    downloaded = 0
    total_size = 0

    for attempt in range(1, max_retries + 1):
        req_headers = dict(base_headers)
        file_mode = "wb"
        if downloaded > 0:
            req_headers["Range"] = f"bytes={downloaded}-"
            file_mode = "ab"

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=req_headers) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status not in (200, 206):
                        return False, f"HTTP Status {resp.status}: {resp.reason}"
                    if resp.status == 200 and downloaded > 0:
                        # Server doesn't support Range; restart from 0
                        downloaded = 0
                        file_mode = "wb"
                    if total_size == 0:
                        total_size = int(resp.headers.get("Content-Length", 0))

                    with open(dest_path, file_mode) as f:
                        async for chunk in resp.content.iter_chunked(2 * 1024 * 1024):
                            task_info = ACTIVE_TASKS.get(task_id)
                            if task_info and task_info.get("cancelled"):
                                raise asyncio.CancelledError("Task was cancelled by user.")
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                await progress_callback(downloaded, total_size, status_msg, "Downloading Link", state, task_id)
                    return True, ""
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Download attempt {attempt} failed: {e}")
            if attempt == max_retries:
                return False, f"Download failed after {max_retries} attempts: {e}"
            await asyncio.sleep(3)

    return False, "Unexpected download termination."

def natural_sort_key(path: str):
    name = os.path.basename(path).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]

def find_rar_outputs(search_dir: str) -> list[str]:
    rar_files = []
    for root, _, files in os.walk(search_dir):
        for f in files:
            if f.lower().endswith(".rar"):
                rar_files.append(os.path.join(root, f))
    rar_files.sort(key=natural_sort_key)
    return rar_files

async def safe_reply_document(message: Message, document: str, caption: str, thumb: str | None, status_msg: Message, progress_label: str, state: dict, task_id: str):
    while True:
        try:
            kwargs = {
                "document": document,
                "caption": caption,
                "progress": progress_callback,
                "progress_args": (status_msg, progress_label, state, task_id)
            }
            if thumb and os.path.exists(thumb):
                kwargs["thumb"] = thumb
            return await message.reply_document(**kwargs)
        except FloodWait as e:
            logger.warning(f"FloodWait of {e.value}s during upload...")
            await asyncio.sleep(e.value + 1)

# ==================== MAIN TASK PIPELINE ====================
async def process_task(task_id: str, should_test: bool):
    task_data = ACTIVE_TASKS.get(task_id)
    if not task_data:
        return

    message = task_data["message"]
    status_msg = task_data["status_msg"]
    mode = task_data["mode"]
    url = task_data.get("url")
    user_id = task_data["user_id"]

    work_dir = os.path.join(WORK_BASE_DIR, f"rar_{uuid.uuid4().hex}")
    output_dir = os.path.join(work_dir, "output")
    task_data["work_dir"] = work_dir
    os.makedirs(output_dir, exist_ok=True)
    stage = "Queue"
    extra_log = ""
    task_start_ts = time.time()

    async with task_semaphore:
        try:
            task_data["running"] = True
            if task_data.get("cancelled"):
                raise asyncio.CancelledError()

            # Disk Space Guard
            expected_size = task_data.get("remote_size", 0)
            free_disk = shutil.disk_usage(WORK_BASE_DIR).free
            if expected_size > 0 and expected_size * 2.2 > free_disk:
                raise RuntimeError(
                    f"Insufficient disk space! File requires ~{human_size(int(expected_size * 2.2))}, "
                    f"but only {human_size(free_disk)} is free on server."
                )

            if mode == "file":
                stage = "Downloading from Telegram"
                await status_msg.edit_text(f"**Status:** {stage}...", reply_markup=get_cancel_markup(task_id))
                state = {"start_time": time.time(), "last_update": 0}

                orig_name = sanitize_filename(task_data.get("orig_filename", "file"))
                save_dest = os.path.join(work_dir, orig_name)

                file_path = await message.download(
                    file_name=save_dest,
                    progress=progress_callback,
                    progress_args=(status_msg, stage, state, task_id)
                )
            else:
                stage = "Downloading URL"
                raw_filename = sanitize_filename(task_data.get("orig_filename", "downloaded_file"))
                file_path = os.path.join(work_dir, raw_filename)

                await status_msg.edit_text(f"**Status:** {stage}...", reply_markup=get_cancel_markup(task_id))
                download_ok, dl_err = await download_stream_url(
                    url, file_path, status_msg, task_id, task_data.get("extra_headers")
                )
                if not download_ok:
                    raise RuntimeError(f"URL download failed: {dl_err}")

            if task_data.get("cancelled"):
                raise asyncio.CancelledError()

            initial_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

            # Smart Extraction Check (APK & package protection)
            ext_mode = task_data.get("extract_mode", "auto")
            extract_dir = os.path.join(work_dir, "extracted")
            extracted = False

            if should_extract_file(file_path, ext_mode):
                stage = "Analyzing and Extracting Archive"
                await status_msg.edit_text(f"**Status:** {stage}...", reply_markup=get_cancel_markup(task_id))
                extracted, ext_log = await try_extract_archive(file_path, extract_dir, task_id)
                extra_log += f"\n--- Extraction Log ---\n{ext_log}"
            else:
                extra_log += f"\n--- Extraction Skipped ---\nFile '{os.path.basename(file_path)}' kept intact (Mode: {ext_mode})."

            if extracted:
                target = extract_dir
                # Progressive Cleanup #1: Delete downloaded archive immediately to save disk space
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            else:
                target = file_path

            if task_data.get("cancelled"):
                raise asyncio.CancelledError()

            stage = f"Compressing to RAR5 (-m{task_data.get('level', 5)})"
            final_name = task_data.get("custom_name") or task_data.get("default_name")
            rar_target = os.path.join(output_dir, f"{final_name}.rar")
            await status_msg.edit_text(f"**Status:** {stage} (0%)...", reply_markup=get_cancel_markup(task_id))
            success, rar_log = await run_rar_compression(target, rar_target, status_msg, task_id)
            extra_log += f"\n--- RAR Log ---\n{rar_log}"

            if not success:
                raise RuntimeError(f"RAR execution failed:\n{rar_log}")

            # Progressive Cleanup #2: Delete source/extracted files before testing & uploading
            try:
                if os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
                elif os.path.exists(target):
                    os.remove(target)
            except Exception:
                pass

            if task_data.get("cancelled"):
                raise asyncio.CancelledError()

            stage = "Scanning for RAR Files"
            generated_parts = find_rar_outputs(output_dir)
            if not generated_parts:
                dir_contents = os.listdir(output_dir)
                raise FileNotFoundError(f"No RAR files found in output.\nDirectory contents: {dir_contents}")

            if should_test:
                stage = "Testing Archive Integrity (rar t)"
                await status_msg.edit_text("**Status:** Running RAR integrity test (`rar t`)...", reply_markup=get_cancel_markup(task_id))
                test_passed, test_log = await run_rar_test(generated_parts[0], task_id)
                extra_log += f"\n--- Test Log ---\n{test_log}"
                if not test_passed:
                    raise RuntimeError(f"RAR Integrity Test Failed:\n{test_log}")
                await status_msg.edit_text("**Status:** RAR Integrity Test Passed (100% OK). Preparing upload...", reply_markup=get_cancel_markup(task_id))
                await asyncio.sleep(1)

            if task_data.get("cancelled"):
                raise asyncio.CancelledError()

            # Upload Stage & Checksums
            stage = "Uploading Part(s)"
            total_compressed_size = sum(os.path.getsize(p) for p in generated_parts)
            thumb_path = get_user_thumb(user_id)
            checksum_lines = []

            for idx, part in enumerate(generated_parts, 1):
                if task_data.get("cancelled"):
                    raise asyncio.CancelledError()

                part_name = os.path.basename(part)
                part_size = os.path.getsize(part)

                if task_data.get("checksum", True):
                    md5_h, sha_h = compute_hashes(part)
                    checksum_lines.append(f"**{part_name}** (`{human_size(part_size)}`)\n`MD5: {md5_h}`\n`SHA256: {sha_h[:32]}...`")

                part_state = {"start_time": time.time(), "last_update": 0}
                caption = (
                    f"**File:** `{part_name}`\n"
                    f"**Part:** `{idx}/{len(generated_parts)}` | **Size:** `{human_size(part_size)}`"
                )
                if task_data.get("password"):
                    caption += f"\n**Password:** `{task_data['password']}`"

                await safe_reply_document(
                    message=message,
                    document=part,
                    caption=caption,
                    thumb=thumb_path,
                    status_msg=status_msg,
                    progress_label=f"Uploading Part {idx}/{len(generated_parts)}",
                    state=part_state,
                    task_id=task_id
                )

            # Final Summary Report
            elapsed_total = time.time() - task_start_ts
            ratio = ((initial_size - total_compressed_size) / initial_size * 100) if initial_size > 0 else 0.0
            ratio_str = f"{ratio:.1f}% saved" if ratio >= 0 else f"{abs(ratio):.1f}% larger"

            summary_text = (
                f"**Task Completed Successfully**\n\n"
                f"- **Archive:** `{final_name}.rar` ({len(generated_parts)} part{'s' if len(generated_parts) > 1 else ''})\n"
                f"- **Original Size:** `{human_size(initial_size)}`\n"
                f"- **Compressed Size:** `{human_size(total_compressed_size)}` (`{ratio_str}`)\n"
                f"- **Total Time:** `{format_time(elapsed_total)}`\n"
                f"- **Integrity Test:** `{'Passed' if should_test else 'Skipped'}`\n"
            )
            if checksum_lines:
                summary_text += "\n**Checksums:**\n" + "\n".join(checksum_lines[:5])
                if len(checksum_lines) > 5:
                    summary_text += f"\n*(+{len(checksum_lines) - 5} more parts)*"

            await status_msg.edit_text(summary_text)

        except asyncio.CancelledError:
            logger.info(f"Task {task_id} cancelled.")
            try:
                await status_msg.edit_text("Task was cancelled by user.")
            except Exception:
                pass
        except Exception as err:
            await send_detailed_error(message, status_msg, stage, err, extra_log)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            ACTIVE_TASKS.pop(task_id, None)
            gc.collect()

# ==================== COMMAND HANDLERS ====================
@app.on_message(filters.command(["start", "help"]))
async def start_handler(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        await message.reply("Access denied.")
        return
    help_text = (
        "**RAR5 Pro Compression Bot**\n\n"
        "**How to use:**\n"
        "1. Send or forward any Telegram file (Document, Video, Audio, APK, etc.).\n"
        "2. Or send a direct download link:\n"
        "   `https://example.com/file.apk`\n"
        "   `https://example.com/file.zip | CustomName | Cookie: a=b`\n\n"
        "**Smart APK/Package Handling:**\n"
        "Files like `.apk`, `.xapk`, `.jar`, `.ipa`, `.docx`, `.epub` are compressed **directly without extraction** in `Auto` mode.\n\n"
        "**Commands:**\n"
        "- `/settings` - Configure your default compression settings\n"
        "- `/setthumb` - Reply to a photo to set custom thumbnail\n"
        "- `/delthumb` - Remove your custom thumbnail\n"
        "- `/status` - Show server CPU, RAM, Disk & Uptime\n"
        "- `/queue` - View active and queued tasks\n"
        "- `/clean` - Force clean temporary directories"
    )
    await message.reply(help_text)

@app.on_message(filters.command("status"))
async def status_cmd_handler(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        return
    st = get_system_stats()
    active_count = sum(1 for t in ACTIVE_TASKS.values() if t.get("running"))
    queued_count = len(ACTIVE_TASKS) - active_count
    text = (
        "**Server & Bot Status**\n\n"
        f"- **Uptime:** `{st['uptime']}`\n"
        f"- **System Load:** `{st['load']}`\n"
        f"- **RAM Used:** `{human_size(st['mem_used'])}` / `{human_size(st['mem_total'])}`\n"
        f"- **Disk Free:** `{human_size(st['disk_free'])}` / `{human_size(st['disk_total'])}` (`{st['disk_percent']}% used`)\n"
        f"- **Tasks:** `{active_count} Running` | `{queued_count} Queued/Setup`"
    )
    await message.reply(text)

@app.on_message(filters.command("queue"))
async def queue_cmd_handler(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        return
    if not ACTIVE_TASKS:
        await message.reply("Queue is currently empty.")
        return
    lines = ["**Current Tasks:**\n"]
    buttons = []
    for tid, tdata in list(ACTIVE_TASKS.items()):
        name = tdata.get("custom_name") or tdata.get("default_name")
        state = "Running" if tdata.get("running") else "Setup / Waiting"
        lines.append(f"- `{tid}`: `{name}.rar` [{state}]")
        if tdata.get("user_id") == message.from_user.id:
            buttons.append([InlineKeyboardButton(f"Cancel {tid} ({name[:15]})", callback_data=f"cancel:{tid}")])
    markup = InlineKeyboardMarkup(buttons) if buttons else None
    await message.reply("\n".join(lines), reply_markup=markup)

@app.on_message(filters.command("clean"))
async def clean_cmd_handler(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        return
    if any(t.get("running") for t in ACTIVE_TASKS.values()):
        await message.reply("Cannot force-clean while a task is actively running. Cancel active tasks first.")
        return
    removed = cleanup_orphaned_dirs()
    gc.collect()
    await message.reply(f"Cleaned `{removed}` temporary workspace directories and freed memory.")

@app.on_message(filters.command("setthumb"))
async def setthumb_handler(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        return
    reply = message.reply_to_message
    if not reply or not (reply.photo or (reply.document and reply.document.mime_type and reply.document.mime_type.startswith("image/"))):
        await message.reply("Reply to a photo with `/setthumb` to save it as your default archive thumbnail.")
        return
    thumb_path = os.path.join(DATA_DIR, f"thumb_{message.from_user.id}.jpg")
    await reply.download(file_name=thumb_path)
    await message.reply("Custom thumbnail saved successfully.")

@app.on_message(filters.command("delthumb"))
async def delthumb_handler(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        return
    thumb_path = get_user_thumb(message.from_user.id)
    if thumb_path and os.path.exists(thumb_path):
        os.remove(thumb_path)
        await message.reply("Custom thumbnail removed.")
    else:
        await message.reply("You don't have a custom thumbnail set.")

def get_user_settings_markup(user_id: int) -> InlineKeyboardMarkup:
    p = get_user_prefs(user_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Default Level: m{p['level']}", callback_data="upref:lvl"),
            InlineKeyboardButton(f"Default Split: {p['split']}", callback_data="upref:split")
        ],
        [
            InlineKeyboardButton(f"Extract Mode: {p['extract_mode'].upper()}", callback_data="upref:ext"),
            InlineKeyboardButton(f"Dict Size: {p['dict_size'].upper()}", callback_data="upref:dict")
        ],
        [
            InlineKeyboardButton(f"Solid: {'ON' if p['solid'] else 'OFF'}", callback_data="upref:solid"),
            InlineKeyboardButton(f"Recovery: {'3%' if p['recovery'] else 'OFF'}", callback_data="upref:rr")
        ],
        [
            InlineKeyboardButton(f"Checksums: {'ON' if p['checksum'] else 'OFF'}", callback_data="upref:chk")
        ]
    ])

@app.on_message(filters.command("settings"))
async def settings_cmd_handler(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        return
    await message.reply(
        "**Your Default Preferences**\nThese settings are automatically applied to every new file or link you send:",
        reply_markup=get_user_settings_markup(message.from_user.id)
    )

@app.on_callback_query(filters.regex(r"^upref:(\w+)$"))
async def upref_callback_handler(client: Client, cq: CallbackQuery):
    uid = cq.from_user.id
    if not is_authorized(uid):
        await cq.answer("Unauthorized.", show_alert=True)
        return
    action = cq.data.split(":")[1]
    p = get_user_prefs(uid)

    if action == "lvl":
        p["level"] = (p["level"] + 1) % 6
    elif action == "split":
        idx = SPLIT_PRESETS.index(p["split"]) if p["split"] in SPLIT_PRESETS else 0
        p["split"] = SPLIT_PRESETS[(idx + 1) % len(SPLIT_PRESETS)]
    elif action == "ext":
        idx = EXTRACT_MODES.index(p["extract_mode"]) if p["extract_mode"] in EXTRACT_MODES else 0
        p["extract_mode"] = EXTRACT_MODES[(idx + 1) % len(EXTRACT_MODES)]
    elif action == "dict":
        idx = DICT_PRESETS.index(p["dict_size"]) if p["dict_size"] in DICT_PRESETS else 0
        p["dict_size"] = DICT_PRESETS[(idx + 1) % len(DICT_PRESETS)]
    elif action == "solid":
        p["solid"] = not p["solid"]
    elif action == "rr":
        p["recovery"] = not p["recovery"]
    elif action == "chk":
        p["checksum"] = not p["checksum"]

    save_prefs()
    await cq.answer("Preference updated!")
    try:
        await cq.message.edit_reply_markup(reply_markup=get_user_settings_markup(uid))
    except MessageNotModified:
        pass

# ==================== FILE & LINK HANDLERS ====================
def create_initial_task_dict(user_id: int, mode: str, message: Message, orig_filename: str, default_name: str, custom_name: str | None, remote_size: int = 0) -> dict:
    prefs = get_user_prefs(user_id)
    return {
        "mode": mode,
        "message": message,
        "orig_filename": orig_filename,
        "default_name": default_name,
        "custom_name": custom_name,
        "remote_size": remote_size,
        "user_id": user_id,
        "level": prefs["level"],
        "split": prefs["split"],
        "dict_size": prefs["dict_size"],
        "solid": prefs["solid"],
        "recovery": prefs["recovery"],
        "encrypt_headers": prefs["encrypt_headers"],
        "extract_mode": prefs["extract_mode"],
        "checksum": prefs["checksum"],
        "password": prefs["default_password"],
        "cancelled": False,
        "running": False,
        "proc": None,
        "async_task": None
    }

@app.on_message(filters.document | filters.video | filters.audio | filters.animation)
async def file_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply("Access denied.")
        return

    task_id = uuid.uuid4().hex[:8]
    file_attr = message.document or message.video or message.audio or message.animation
    orig_name = getattr(file_attr, "file_name", None) or f"telegram_media_{task_id}"
    file_size = getattr(file_attr, "file_size", 0) or 0
    default_name = sanitize_filename(strip_known_archive_ext(orig_name))

    custom_name = None
    if message.caption:
        first_line = message.caption.strip().split("\n")[0].strip()
        clean_caption = sanitize_filename(first_line)
        if clean_caption.lower().endswith(".rar"):
            clean_caption = clean_caption[:-4].strip()
        if clean_caption:
            custom_name = clean_caption

    task_data = create_initial_task_dict(
        user_id=user_id,
        mode="file",
        message=message,
        orig_filename=orig_name,
        default_name=default_name,
        custom_name=custom_name,
        remote_size=file_size
    )
    ACTIVE_TASKS[task_id] = task_data

    prompt = await message.reply(
        get_setup_text(task_data),
        reply_markup=get_setup_markup(task_id)
    )
    task_data["status_msg"] = prompt

@app.on_message(filters.regex(r"^https?://"))
async def link_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply("Access denied.")
        return

    url, custom_name, extra_headers = parse_link_message(message.text)
    status_msg = await message.reply("Validating URL status...")

    is_valid, err_msg, detected_filename, content_size = await validate_url(url, extra_headers)
    if not is_valid:
        await status_msg.edit_text(f"**URL Validation Failed:**\n`{err_msg}`")
        return

    task_id = uuid.uuid4().hex[:8]
    resolved_filename = detected_filename or "downloaded_file"
    default_name = sanitize_filename(strip_known_archive_ext(resolved_filename))

    task_data = create_initial_task_dict(
        user_id=user_id,
        mode="url",
        message=message,
        orig_filename=resolved_filename,
        default_name=default_name,
        custom_name=custom_name,
        remote_size=content_size
    )
    task_data["url"] = url
    task_data["extra_headers"] = extra_headers
    task_data["status_msg"] = status_msg
    ACTIVE_TASKS[task_id] = task_data

    await status_msg.edit_text(
        get_setup_text(task_data),
        reply_markup=get_setup_markup(task_id)
    )

# ==================== INTERACTIVE SETUP CALLBACKS ====================
@app.on_callback_query(filters.regex(r"^cfg:(lvl|split|ext|solid|rr|dict|enc):([a-f0-9]+)$"))
async def config_toggle_callback(client: Client, cq: CallbackQuery):
    _, field, task_id = cq.data.split(":")
    task_data = ACTIVE_TASKS.get(task_id)

    if not task_data:
        await cq.answer("Task expired or not found.", show_alert=True)
        return
    if cq.from_user.id != task_data["user_id"]:
        await cq.answer("Unauthorized.", show_alert=True)
        return

    if field == "lvl":
        task_data["level"] = (task_data["level"] + 1) % 6
    elif field == "split":
        cur = task_data["split"]
        idx = SPLIT_PRESETS.index(cur) if cur in SPLIT_PRESETS else 0
        task_data["split"] = SPLIT_PRESETS[(idx + 1) % len(SPLIT_PRESETS)]
    elif field == "ext":
        cur = task_data["extract_mode"]
        idx = EXTRACT_MODES.index(cur) if cur in EXTRACT_MODES else 0
        task_data["extract_mode"] = EXTRACT_MODES[(idx + 1) % len(EXTRACT_MODES)]
    elif field == "solid":
        task_data["solid"] = not task_data["solid"]
    elif field == "rr":
        task_data["recovery"] = not task_data["recovery"]
    elif field == "dict":
        cur = task_data["dict_size"]
        idx = DICT_PRESETS.index(cur) if cur in DICT_PRESETS else 0
        task_data["dict_size"] = DICT_PRESETS[(idx + 1) % len(DICT_PRESETS)]
    elif field == "enc":
        task_data["encrypt_headers"] = not task_data["encrypt_headers"]

    await cq.answer()
    try:
        await task_data["status_msg"].edit_text(
            get_setup_text(task_data),
            reply_markup=get_setup_markup(task_id)
        )
    except MessageNotModified:
        pass

@app.on_callback_query(filters.regex(r"^input:(rename|password|split):([a-f0-9]+)$"))
async def input_request_callback(client: Client, cq: CallbackQuery):
    _, input_type, task_id = cq.data.split(":")
    task_data = ACTIVE_TASKS.get(task_id)

    if not task_data:
        await cq.answer("Task expired or not found.", show_alert=True)
        return
    if cq.from_user.id != task_data["user_id"]:
        await cq.answer("Unauthorized.", show_alert=True)
        return

    AWAITING_INPUT[cq.from_user.id] = {"task_id": task_id, "type": input_type}
    await cq.answer()

    prompts = {
        "rename": "Send the **new archive name** in chat (without `.rar` extension):",
        "password": "Send the **archive password** in chat (or send `none` to clear password):",
        "split": "Send the **split volume size** (e.g. `700m`, `2g`, or `0` for no split):"
    }
    back_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Back to Setup", callback_data=f"back:{task_id}")],
        [InlineKeyboardButton("Cancel Task", callback_data=f"cancel:{task_id}")]
    ])
    await task_data["status_msg"].edit_text(prompts[input_type], reply_markup=back_markup)

@app.on_callback_query(filters.regex(r"^back:([a-f0-9]+)$"))
async def back_to_setup_callback(client: Client, cq: CallbackQuery):
    task_id = cq.data.split(":")[1]
    task_data = ACTIVE_TASKS.get(task_id)
    AWAITING_INPUT.pop(cq.from_user.id, None)
    if not task_data:
        await cq.answer("Task expired.", show_alert=True)
        return
    await cq.answer()
    await task_data["status_msg"].edit_text(
        get_setup_text(task_data),
        reply_markup=get_setup_markup(task_id)
    )

@app.on_message(filters.text & ~filters.regex(r"^/"))
async def text_input_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if user_id not in AWAITING_INPUT:
        return

    wait_info = AWAITING_INPUT.pop(user_id)
    task_id = wait_info["task_id"]
    input_type = wait_info["type"]
    task_data = ACTIVE_TASKS.get(task_id)

    if not task_data:
        await message.reply("Task expired or not found.")
        return

    raw_input = message.text.strip().split("\n")[0].strip()

    if input_type == "rename":
        clean_name = sanitize_filename(raw_input)
        if clean_name.lower().endswith(".rar"):
            clean_name = clean_name[:-4].strip()
        task_data["custom_name"] = clean_name or task_data["default_name"]

    elif input_type == "password":
        if raw_input.lower() in ("none", "off", "clear", "-"):
            task_data["password"] = None
        else:
            task_data["password"] = raw_input

    elif input_type == "split":
        if re.match(r"^\d+[kmgKMG]?$", raw_input):
            task_data["split"] = raw_input.lower()

    try:
        await message.delete()
    except Exception:
        pass

    await task_data["status_msg"].edit_text(
        get_setup_text(task_data),
        reply_markup=get_setup_markup(task_id)
    )

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

    AWAITING_INPUT.pop(callback_query.from_user.id, None)
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

@app.on_callback_query(filters.regex(r"^start:(yes|no):([a-f0-9]+)$"))
async def start_task_callback(client: Client, callback_query: CallbackQuery):
    _, action, task_id = callback_query.data.split(":")
    task_data = ACTIVE_TASKS.get(task_id)

    if not task_data:
        await callback_query.answer("Task expired or not found.", show_alert=True)
        return

    if callback_query.from_user.id != task_data["user_id"]:
        await callback_query.answer("Unauthorized.", show_alert=True)
        return

    AWAITING_INPUT.pop(callback_query.from_user.id, None)
    await callback_query.answer("Task started!")
    should_test = (action == "yes")
    await task_data["status_msg"].edit_text("Task queued...", reply_markup=get_cancel_markup(task_id))

    t = asyncio.create_task(process_task(task_id, should_test))
    task_data["async_task"] = t

# ==================== WEB SERVER & LIFECYCLE ====================
async def start_web_health():
    app_web = web.Application()

    async def root_handler(request):
        return web.Response(text="RAR5 Pro Bot is running.")

    async def health_handler(request):
        st = get_system_stats()
        st["active_tasks"] = len(ACTIVE_TASKS)
        return web.json_response(st)

    app_web.router.add_get("/", root_handler)
    app_web.router.add_get("/health", health_handler)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health server listening on port {PORT}")

async def shutdown_cleanup():
    logger.info("Shutting down... terminating active subprocesses.")
    for tid, tdata in list(ACTIVE_TASKS.items()):
        tdata["cancelled"] = True
        proc = tdata.get("proc")
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        work_dir = tdata.get("work_dir")
        if work_dir and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

async def main():
    load_prefs()
    cleaned = cleanup_orphaned_dirs()
    if cleaned:
        logger.info(f"Cleaned {cleaned} orphaned directories on startup.")
    await start_web_health()
    await app.start()
    logger.info("RAR5 Pro Bot started successfully.")
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()
    await shutdown_cleanup()
    await app.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
