import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import yt_dlp

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "49"))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "180"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("pinterest_downloader")

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
ALLOWED_HOSTS = {
    "pinterest.com",
    "www.pinterest.com",
    "m.pinterest.com",
    "pin.it",
}


def get_pinterest_url(text: str) -> str | None:
    """Return a Pinterest URL only if its hostname is on the allowlist."""
    match = URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,!?;:)]}'\"")
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        return None
    return url


def _download_with_ytdlp(url: str, output_dir: str) -> tuple[list[str], dict]:
    """
    Download media supported by yt-dlp from a public Pinterest URL.
    Returns downloaded file paths and basic metadata.
    """
    output_template = str(Path(output_dir) / "%(title).80s-%(id)s.%(ext)s")
    opts = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": MAX_FILE_SIZE,
        # هم ویدیو و هم عکس را پشتیبانی می‌کند
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = []
    for path in Path(output_dir).iterdir():
        if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".json"}:
            files.append(str(path))

    if isinstance(info, dict) and info.get("entries"):
        entries = [entry for entry in info["entries"] if entry]
        title = (
            entries[0].get("title", "Pinterest media")
            if entries
            else "Pinterest media"
        )
    else:
        title = (
            info.get("title", "Pinterest media")
            if isinstance(info, dict)
            else "Pinterest media"
        )

    return files, {"title": title}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "سلام! 👋\n\n"
        "لینک عمومی یک پین از Pinterest را بفرست تا رسانهٔ قابل‌دانلودش را بررسی کنم.\n\n"
        "• از لینک‌های pinterest.com و pin.it پشتیبانی می‌شود.\n"
        f"• سقف هر فایل: {MAX_FILE_SIZE_MB} مگابایت\n"
        "• فقط محتوایی که به‌طور عمومی و مجاز در دسترس است.\n\n"
        "برای راهنما /help را بزن."
    )
    await update.effective_message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "روش استفاده:\n"
        "۱) لینک یک پین عمومی را کپی کن.\n"
        "۲) لینک را همین‌جا بفرست.\n"
        "۳) منتظر بمان تا فایل قابل‌دسترسی ارسال شود.\n\n"
        "نکته: همهٔ پین‌ها یا انواع رسانه الزاماً توسط yt-dlp پشتیبانی نمی‌شوند. "
        "لینک‌های خصوصی، محتوای نیازمند ورود یا لینک‌های حذف‌شده ممکن است کار نکنند. "
        "از دانلود و بازنشر محتوایی که اجازه‌اش را نداری خودداری کن."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    url = get_pinterest_url(message.text)
    if not url:
        await message.reply_text(
            "لطفاً یک لینک معتبر Pinterest بفرست؛ مثل لینک‌های pinterest.com یا pin.it."
        )
        return

    status = await message.reply_text(
        "🔎 دارم لینک را بررسی و رسانهٔ قابل‌دانلود را دریافت می‌کنم…"
    )
    chat_id = update.effective_chat.id

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        with tempfile.TemporaryDirectory(prefix="pinterest_dl_") as temp_dir:
            try:
                files, metadata = await asyncio.wait_for(
                    asyncio.to_thread(_download_with_ytdlp, url, temp_dir),
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await status.edit_text(
                    "⏱️ دانلود بیش از حد طول کشید. لطفاً دوباره امتحان کن."
                )
                return
            except yt_dlp.utils.DownloadError as exc:
                logger.info("yt-dlp download failed (%s): %s", type(exc).__name__, exc)
                await status.edit_text(
                    "❌ نتوانستم این پین را دانلود کنم.\n"
                    "ممکن است لینک خصوصی، حذف‌شده، فقط عکس بدون فرمت پشتیبانی‌شده، "
                    "یا توسط yt-dlp پشتیبانی‌نشده باشد.\n"
                    "yt-dlp را به‌روز کن و دوباره امتحان کن."
                )
                return
            except Exception as exc:
                logger.exception("Unexpected download error: %s", type(exc).__name__)
                await status.edit_text(
                    "❌ هنگام دریافت رسانه خطایی رخ داد. بعداً دوباره امتحان کن."
                )
                return

            if not files:
                await status.edit_text(
                    "برای این لینک فایل رسانه‌ای پیدا نکردم. "
                    "ممکن است این نوع پین پشتیبانی نشود."
                )
                return

            sent_count = 0
            for file_path in files:
                path = Path(file_path)
                if not path.exists() or not path.is_file():
                    continue

                size = path.stat().st_size
                if size == 0:
                    continue

                if size > MAX_FILE_SIZE:
                    await message.reply_text(
                        f"⚠️ فایل «{path.name}» از سقف {MAX_FILE_SIZE_MB} مگابایت "
                        "بزرگ‌تر است و ارسال نشد."
                    )
                    continue

                caption = (metadata.get("title") or "Pinterest media")[:900]

                await context.bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT
                )

                suffix = path.suffix.lower()
                if suffix in {".mp4", ".m4v", ".mov", ".webm"}:
                    with path.open("rb") as media:
                        await message.reply_video(
                            video=media,
                            caption=caption if sent_count == 0 else None,
                            supports_streaming=True,
                            read_timeout=60,
                            write_timeout=60,
                            connect_timeout=30,
                        )
                elif suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                    with path.open("rb") as media:
                        await message.reply_document(
                            document=media,
                            filename=path.name,
                            caption=caption if sent_count == 0 else None,
                            read_timeout=60,
                            write_timeout=60,
                            connect_timeout=30,
                        )
                else:
                    with path.open("rb") as media:
                        await message.reply_document(
                            document=media,
                            filename=path.name,
                            caption=caption if sent_count == 0 else None,
                            read_timeout=60,
                            write_timeout=60,
                            connect_timeout=30,
                        )

                sent_count += 1

            if sent_count:
                await status.delete()
            else:
                await status.edit_text(
                    "فایل قابل‌ارسالی پیدا نشد یا همهٔ فایل‌ها از سقف حجم بزرگ‌تر بودند."
                )

    except Exception as exc:
        logger.exception("Handler error: %s", type(exc).__name__)
        try:
            await status.edit_text(
                "❌ خطای غیرمنتظره‌ای رخ داد. لطفاً بعداً دوباره امتحان کن."
            )
        except Exception:
            pass


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "دستور ناشناخته است. برای راهنما /help را بزن."
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set it in your .env or environment variables."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("Pinterest downloader bot is starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
