import os
import re
import time
import secrets
import logging
import asyncio
import threading
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv
import yt_dlp
from yt_dlp.version import __version__ as YTDLP_VERSION

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# تنظیمات عضویت اجباری از طریق ربات ادمین مرکزی
ADMIN_API_URL = os.getenv("ADMIN_API_URL", "https://admin-dl-bot.onrender.com").rstrip("/")
API_KEY = os.getenv("API_KEY", "")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("yt-dlp version: %s", YTDLP_VERSION)

MAX_FILE_SIZE = 49 * 1024 * 1024
MAX_PHOTO_SIZE = 10 * 1024 * 1024

MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "10"))
_last_request: dict[int, float] = {}

PENDING: dict[str, dict] = {}
PENDING_TTL = 30 * 60

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ⚠️ متن دکمه استارت — همه‌جا از همین استفاده می‌شود
START_BUTTON_TEXT = "🚀 استارت"


# ---------------- Health Server ----------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Pinterest bot is alive")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server started on port %s", port)
    server.serve_forever()


# ---------------- Bot Commands (منوی آبی کنار کیبورد) ----------------

async def setup_bot_commands(app: Application):
    commands = [
        BotCommand("start", START_BUTTON_TEXT),
    ]

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeDefault(),
    )

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllPrivateChats(),
    )

    logger.info("Bot commands configured.")


# ---------------- Reply Keyboard (کیبورد پایین) ----------------

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(START_BUTTON_TEXT)],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="لینک پین‌ترست را بفرست یا استارت بزن...",
    )


# ---------------- URL helpers ----------------

PINTEREST_HOST_RE = re.compile(r"(^|\.)pinterest\.[a-z.]+$")
PIN_ID_RE = re.compile(r"/pin/(?:[^/]*--)?(\d+)")


def extract_url(text: str):
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None


def is_short_link(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (parsed.hostname or "").lower() == "pin.it" and len(parsed.path) > 1
    except Exception:
        return False


def parse_pin_id(url: str):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not PINTEREST_HOST_RE.search(host):
            return None
        match = PIN_ID_RE.search(parsed.path)
        return match.group(1) if match else None
    except Exception:
        return None


async def resolve_short_link(url: str):
    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers=BROWSER_HEADERS
        ) as client:
            resp = await client.get(url)
            return str(resp.url)
    except Exception as e:
        logger.warning("Short link resolve failed: %s", e)
        return None


# ---------------- Pin page (Open Graph) ----------------

class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = dict(attrs)
        key = a.get("property") or a.get("name")
        content = a.get("content")
        if key and content and key not in self.meta:
            self.meta[key] = content


def parse_meta(html: str) -> dict:
    parser = MetaParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.meta


async def fetch_pin_meta(pin_url: str) -> dict:
    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers=BROWSER_HEADERS
        ) as client:
            resp = await client.get(pin_url)
            if resp.status_code != 200:
                return {}
            return parse_meta(resp.text)
    except Exception as e:
        logger.warning("Pin page fetch failed: %s", e)
        return {}


def to_original_url(image_url: str) -> str:
    return re.sub(r"(i\.pinimg\.com)/\d+x/", r"\1/originals/", image_url, count=1)


def build_caption(meta: dict, pin_url: str) -> str:
    parts = []
    title = (meta.get("og:title") or "").strip()
    desc = (meta.get("og:description") or "").strip()
    if title:
        parts.append(title)
    if desc and desc != title:
        parts.append(desc)
    text = "\n\n".join(parts)
    if len(text) > 800:
        text = text[:797] + "..."
    link = meta.get("og:url") or pin_url
    return f"{text}\n\n{link}".strip()[:1024]


# ---------------- Image sending ----------------

async def download_bytes(url: str, max_bytes: int):
    try:
        async with httpx.AsyncClient(
            timeout=60, follow_redirects=True, headers=BROWSER_HEADERS
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                length = resp.headers.get("content-length")
                if length and int(length) > max_bytes:
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        return None
                return bytes(buf)
    except Exception:
        logger.warning("download_bytes failed for %s", url, exc_info=True)
        return None


async def send_image(context, chat_id: int, image_url: str, caption: str) -> bool:
    candidates = []
    original = to_original_url(image_url)
    if original != image_url:
        candidates.append(original)
    candidates.append(image_url)

    for url in candidates:
        is_gif = urlparse(url).path.lower().endswith(".gif")
        limit = MAX_FILE_SIZE if is_gif else 40 * 1024 * 1024
        data = await download_bytes(url, limit)
        if not data:
            continue

        name = Path(urlparse(url).path).name or "pin.jpg"

        try:
            if is_gif:
                await context.bot.send_animation(
                    chat_id=chat_id, animation=data, filename=name, caption=caption
                )
                return True

            if len(data) <= MAX_PHOTO_SIZE:
                try:
                    await context.bot.send_photo(
                        chat_id=chat_id, photo=data, caption=caption
                    )
                    return True
                except Exception:
                    logger.warning("send_photo failed; sending as document", exc_info=True)

            await context.bot.send_document(
                chat_id=chat_id, document=data, filename=name, caption=caption
            )
            return True
        except Exception:
            logger.warning("Sending image failed for %s", url, exc_info=True)

    return False


# ---------------- yt-dlp helpers (video pins) ----------------

VIDEO_EXTS = {"mp4", "m4v", "mov", "webm"}


def base_opts(tmpdir: str) -> dict:
    return {
        "outtmpl": str(Path(tmpdir) / "video.%(ext)s"),
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 30,
    }


def estimate_size(fmt: dict, duration):
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    tbr = fmt.get("tbr")
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None


def build_quality_options(info: dict) -> dict:
    entries = info.get("entries")
    if entries:
        entries = [e for e in entries if e]
        if entries:
            info = entries[0]

    duration = info.get("duration")
    best_per_height: dict[int, dict] = {}

    for f in info.get("formats") or []:
        protocol = (f.get("protocol") or "").lower()
        if not protocol.startswith("http") or "m3u8" in protocol:
            continue
        if f.get("vcodec") == "none":
            continue
        if (f.get("ext") or "").lower() not in VIDEO_EXTS:
            continue
        height = f.get("height")
        if not height:
            continue
        size = estimate_size(f, duration)
        if size is not None and size > MAX_FILE_SIZE:
            continue
        prev = best_per_height.get(height)
        if prev is None or (f.get("tbr") or 0) > (prev.get("tbr") or 0):
            best_per_height[height] = {
                "format_id": f["format_id"],
                "height": int(height),
                "width": f.get("width"),
                "size": size,
                "tbr": f.get("tbr"),
            }

    return {
        v["format_id"]: v
        for v in sorted(best_per_height.values(), key=lambda x: x["height"])
    }


def cleanup_pending():
    now = time.time()
    for token in [t for t, e in PENDING.items() if now - e["ts"] > PENDING_TTL]:
        PENDING.pop(token, None)


def human_size(size):
    return f"{size / 1024 / 1024:.1f} MB" if size else "?"


async def get_video_options(url: str):
    try:
        with TemporaryDirectory() as tmpdir:
            opts = base_opts(tmpdir)
            opts["skip_download"] = True

            def extract():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)

            info = await asyncio.to_thread(extract)

        options = build_quality_options(info)
        if options:
            return info, options
    except Exception as e:
        logger.info("No video options (probably an image pin): %s", str(e)[:200])
    return None


# ---------------- Mandatory Join (Central Admin API) ----------------

async def check_membership(user_id: int):
    if not API_KEY:
        logger.error("API_KEY is not configured; mandatory join check is blocked")
        return False, []

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                f"{ADMIN_API_URL}/api/check",
                params={"user_id": user_id},
                headers={"X-API-Key": API_KEY},
            )
            response.raise_for_status()
            data = response.json()

        if data.get("enabled") is False:
            return True, []

        missing = data.get("missing") or []
        allowed = bool(data.get("ok")) and not missing
        return allowed, missing
    except Exception:
        logger.exception("Central membership API request failed")
        return False, []


def _channel_value(channel, *keys):
    if isinstance(channel, dict):
        for key in keys:
            value = channel.get(key)
            if value:
                return str(value)
        return ""
    return str(channel) if channel else ""


async def membership_markup(missing):
    rows = []
    channels = missing if all(isinstance(item, dict) for item in missing) else []
    if missing and not channels:
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(
                    f"{ADMIN_API_URL}/api/channels",
                    headers={"X-API-Key": API_KEY},
                )
                response.raise_for_status()
                payload = response.json()
            all_channels = payload.get("channels", payload) if isinstance(payload, dict) else payload
            if isinstance(all_channels, list):
                wanted = {str(item).lstrip("@").lower() for item in missing}
                for channel in all_channels:
                    if not isinstance(channel, dict):
                        continue
                    identifiers = {
                        _channel_value(channel, "id", "channel_id", "username", "channel", "chat_id").lstrip("@").lower(),
                        _channel_value(channel, "username", "channel", "chat_id").lstrip("@").lower(),
                    }
                    if wanted.intersection(identifiers):
                        channels.append(channel)
        except Exception:
            logger.exception("Could not load channel buttons from central API")

    for channel in channels:
        url = _channel_value(channel, "join_url", "url", "invite_link", "channel_url", "link")
        name = _channel_value(channel, "title", "name", "username", "channel") or "عضویت در کانال"
        if url.startswith("http://") or url.startswith("https://"):
            rows.append([InlineKeyboardButton(f"عضویت در {name}", url=url)])

    rows.append([InlineKeyboardButton("✅ عضو شدم؛ بررسی دوباره", callback_data="join:check")])
    return InlineKeyboardMarkup(rows)


async def require_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False

    allowed, missing = await check_membership(user.id)
    if allowed:
        return True

    await message.reply_text(
        "🔒 برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شو؛ سپس روی «عضو شدم» بزن.",
        reply_markup=await membership_markup(missing),
    )
    return False


async def on_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    allowed, missing = await check_membership(query.from_user.id)
    if allowed:
        await query.edit_message_text(
            "✅ عضویتت تأیید شد. حالا لینک پینترست را برای ربات بفرست."
        )
    else:
        await query.edit_message_text(
            "❌ هنوز عضویتت تأیید نشده. در کانال‌های لازم عضو شو و دوباره بررسی کن.",
            reply_markup=await membership_markup(missing),
        )


# ---------------- Bot Commands ----------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await require_membership(update, context):
        return
    # ⚠️ کیبورد پایین همیشه همراه پیام استارت ارسال می‌شود
    await update.effective_message.reply_text(
        "سلام 👋\n\n"
        "لینک یک پین پینترست (pinterest.com/pin/... یا pin.it/...) را بفرست.\n"
        "اگر عکس یا GIF باشد با بهترین کیفیت می‌فرستمش، و اگر ویدئو باشد "
        "می‌توانی کیفیت را انتخاب کنی. 📌\n\n"
        "فقط محتوایی را دانلود کن که مال خودت است یا اجازه‌اش را داری.",
        reply_markup=get_main_reply_keyboard(),
    )


# ---------------- هندلر دکمه استارت از کیبورد پایین ----------------

async def handle_reply_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message or not message.text:
        return

    if message.text.strip() == START_BUTTON_TEXT:
        await start(update, context)


async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not message.text:
        return

    if not await require_membership(update, context):
        return

    raw_url = extract_url(message.text)

    if not raw_url:
        await message.reply_text(
            "❌ لطفاً لینک یک پین پینترست بفرست.\n"
            "نمونه: pinterest.com/pin/123456789 یا pin.it/abc123"
        )
        return

    user_id = update.effective_user.id if update.effective_user else 0
    now = time.monotonic()
    last = _last_request.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last)) + 1
        await message.reply_text(f"⏳ لطفاً {wait} ثانیه‌ی دیگر دوباره تلاش کن.")
        return

    status = await message.reply_text("⏳ در حال بررسی پین...")
    chat_id = message.chat_id

    try:
        url = raw_url
        if is_short_link(url):
            url = await resolve_short_link(url) or url

        pin_id = parse_pin_id(url)
        if not pin_id:
            await status.edit_text(
                "❌ این لینک یک پین پینترست نیست.\n"
                "(لینک برد، پروفایل و جستجو پشتیبانی نمی‌شود.)"
            )
            return

        _last_request[user_id] = now

        pin_url = f"https://www.pinterest.com/pin/{pin_id}/"
        meta = await fetch_pin_meta(pin_url)
        caption = build_caption(meta, pin_url)

        video = await get_video_options(pin_url)
        if video:
            info, options = video
            cleanup_pending()
            token = secrets.token_hex(4)
            entries = info.get("entries")
            meta_info = [e for e in entries if e][0] if entries and any(entries) else info
            PENDING[token] = {
                "url": pin_url,
                "user_id": user_id,
                "options": options,
                "duration": meta_info.get("duration"),
                "caption": caption,
                "ts": time.time(),
                "busy": False,
            }
            buttons = [
                InlineKeyboardButton(
                    f"{opt['height']}p  ({human_size(opt['size'])})",
                    callback_data=f"q:{token}:{fid}",
                )
                for fid, opt in options.items()
            ]
            markup = InlineKeyboardMarkup([[b] for b in buttons])
            await status.edit_text("🎬 کیفیت را انتخاب کن:", reply_markup=markup)
            return

        image_url = meta.get("og:image") or meta.get("twitter:image")
        if image_url:
            await status.edit_text("📤 در حال ارسال...")
            async with DOWNLOAD_SEMAPHORE:
                ok = await send_image(context, chat_id, image_url, caption)
            if ok:
                await status.delete()
                return

        await status.edit_text(
            "❌ محتوای این پین پیدا یا ارسال نشد.\n"
            "ممکن است پین خصوصی یا حذف‌شده باشد، یا پینترست دسترسی ربات را محدود کرده باشد."
        )

    except Exception as e:
        logger.exception("Pin handling failed")
        await status.edit_text(f"❌ خطا:\n{str(e)[:700]}")


async def on_quality(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    data = query.data or ""

    allowed, missing = await check_membership(query.from_user.id)
    if not allowed:
        await query.answer("ابتدا باید در کانال‌های لازم عضو شوی.", show_alert=True)
        await query.message.reply_text(
            "🔒 برای ادامه، ابتدا عضو کانال‌های زیر شو و عضویتت را بررسی کن.",
            reply_markup=await membership_markup(missing),
        )
        return

    try:
        _, token, fid = data.split(":", 2)
    except ValueError:
        await query.answer()
        return

    entry = PENDING.get(token)
    option = entry["options"].get(fid) if entry else None

    if not entry or not option:
        await query.answer()
        await query.edit_message_text(
            "⌛ این درخواست منقضی شده؛ لینک پین را دوباره بفرست."
        )
        return

    if query.from_user.id != entry["user_id"]:
        await query.answer("این دکمه برای تو نیست.", show_alert=True)
        return

    if entry["busy"]:
        await query.answer("در حال انجام است...")
        return

    entry["busy"] = True
    await query.answer()
    await query.edit_message_text(f"⏳ در حال دانلود ({option['height']}p)...")

    chat_id = query.message.chat_id

    try:
        async with DOWNLOAD_SEMAPHORE:
            with TemporaryDirectory() as tmpdir:
                opts = base_opts(tmpdir)
                opts["format"] = fid

                def download():
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(entry["url"], download=True)

                info = await asyncio.to_thread(download)

                entries = info.get("entries")
                if entries and any(entries):
                    info = [e for e in entries if e][0]

                files = [
                    p for p in Path(tmpdir).iterdir()
                    if p.is_file()
                    and p.suffix.lower().lstrip(".") in VIDEO_EXTS
                    and p.stat().st_size > 0
                ]

                if not files:
                    raise FileNotFoundError("Downloaded video was not found")

                filename = max(files, key=lambda p: p.stat().st_size)

                if filename.stat().st_size > MAX_FILE_SIZE:
                    await query.edit_message_text(
                        "❌ حجم این ویدئو از سقف ارسال ربات (حدود ۵۰ مگابایت) بیشتر است."
                    )
                    entry["busy"] = False
                    return

                await query.edit_message_text("📤 در حال ارسال...")

                with filename.open("rb") as video:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=video,
                        supports_streaming=True,
                        duration=int(info.get("duration") or entry.get("duration") or 0) or None,
                        width=option.get("width"),
                        height=option.get("height"),
                        caption=entry.get("caption") or "🎬",
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

        await query.message.delete()
        PENDING.pop(token, None)

    except Exception as e:
        logger.exception("Pin video download failed")
        entry["busy"] = False
        await query.edit_message_text(f"❌ خطا:\n{str(e)[:700]}")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logger.warning("Bot error: %s", context.error)


# ---------------- Main ----------------

async def post_init(app: Application):
    await setup_bot_commands(app)


def main():
    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_join_check, pattern=r"^join:check$"))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q:"))

    # ⚠️ هندلر دکمه استارت از کیبورد پایین — دقیقاً قبل از handle_url
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(
                rf"^{re.escape(START_BUTTON_TEXT)}$"
            ),
            handle_reply_keyboard,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )

    app.add_error_handler(on_error)

    logger.info("Pinterest bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
