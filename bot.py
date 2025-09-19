import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp

# -------------------- ENV --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
CHANNEL_ID = os.getenv("CHANNEL_ID")
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in .env")

# -------------------- BOT CORE --------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

TELEGRAM_BOT_FILE_LIMIT_MB = 48
SUPPORTED_URL_RE = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be|tiktok\.com)/\S+)",
    re.IGNORECASE,
)

@dataclass
class PendingJob:
    url: str
    user_id: int

pending: dict[int, PendingJob] = {}


# -------------------- HELPERS --------------------
async def is_subscribed(user_id: int) -> bool:
    if not CHANNEL_USERNAME and not CHANNEL_ID:
        return True
    chat = CHANNEL_USERNAME or int(CHANNEL_ID)
    try:
        m = await bot.get_chat_member(chat_id=chat, user_id=user_id)
        return getattr(m, "status", None) in ("member", "administrator", "creator")
    except Exception:
        return False


def human_size(n: int) -> str:
    size, units = float(n), ["B", "KB", "MB", "GB"]
    for u in units:
        if size < 1024 or u == "GB":
            return f"{size:.1f}{u}"
        size /= 1024


def build_main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎬 Відео (краща якість)", callback_data="fmt:best")
    kb.button(text="🎥 Відео 720p", callback_data="fmt:720")
    kb.button(text="📱 Відео 360p", callback_data="fmt:360")
    kb.button(text="🎧 Аудіо (MP3)", callback_data="fmt:audio")
    kb.adjust(1)
    return kb.as_markup()


# -------- yt-dlp options (PROBE vs DOWNLOAD) --------
def _common_opts(out_dir: str, player_client: str) -> dict:
    opts = {
        "outtmpl": os.path.join(out_dir, "%(title).100s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        # повністю ігноруємо будь-які зовнішні конфіги/alias/ярлики
        "ignoreconfig": True,      # --ignore-config
        "config_locations": [],    # навіть якщо десь задані
        # обраний клієнт YouTube
        "extractor_args": {"youtube": {"player_client": [player_client]}},
        # пробуємо кілька разів на випадок мережевих флуктуацій
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 15,
    }
    if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
        opts["cookiefile"] = YT_COOKIES_FILE
    return opts


def ydl_probe_opts(out_dir: str, player_client: str) -> dict:
    return {
        **_common_opts(out_dir, player_client),
        "skip_download": True,  # ключове: тільки метадані
        # НІЯКИХ format/postprocessors/merge_output_format тут!
    }


def ydl_download_opts(out_dir: str, fmt_string: str, choice: str, player_client: str) -> dict:
    opts = {
        **_common_opts(out_dir, player_client),
        "format": fmt_string,
        "merge_output_format": "mp4",
        "postprocessors": [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},  # (так, саме preferedformat)
        ],
    }
    if choice == "audio":
        opts["postprocessors"].append(
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        )
    return opts


# -------- підбір реальних форматів --------
def _best_by(items: List[Dict[str, Any]], keys: List[str], reverse: bool = True):
    def k(f: Dict[str, Any]):
        vals = []
        for key in keys:
            v = f.get(key)
            if v is None:
                v = -1 if reverse else 10**12
            vals.append(v)
        return tuple(vals)
    return sorted(items, key=k, reverse=reverse)[0] if items else None


def pick_format_string(info: Dict[str, Any], choice: str) -> Tuple[str, str]:
    fmts: List[Dict[str, Any]] = info.get("formats") or []
    if not fmts:
        raise RuntimeError("No formats list from extractor")

    progressive = [f for f in fmts if f.get("vcodec") != "none" and f.get("acodec") != "none"]
    video_only  = [f for f in fmts if f.get("vcodec") != "none" and (f.get("acodec") in (None, "none"))]
    audio_only  = [f for f in fmts if f.get("acodec") != "none" and (f.get("vcodec") in (None, "none"))]

    def best_prog(max_h: Optional[int] = None):
        arr = progressive
        if max_h is not None:
            arr = [f for f in arr if (f.get("height") or 0) <= max_h or f.get("height") is None]
        mp4 = [f for f in arr if f.get("ext") == "mp4"]
        return _best_by(mp4, ["height", "tbr"]) or _best_by(arr, ["height", "tbr"])

    def best_vo(max_h: Optional[int] = None):
        arr = video_only
        if max_h is not None:
            arr = [f for f in arr if (f.get("height") or 0) <= max_h or f.get("height") is None]
        return _best_by(arr, ["height", "tbr"])

    def best_ao():
        m4a = [f for f in audio_only if f.get("ext") in ("m4a", "mp4")]
        return _best_by(m4a, ["abr", "tbr"]) or _best_by(audio_only, ["abr", "tbr"])

    if choice == "audio":
        a = best_ao()
        if a:
            return a["format_id"], f"audio-only {a.get('ext')} ~{a.get('abr') or a.get('tbr')}kbps"
        p = best_prog()
        if p:
            return p["format_id"], f"progressive {p.get('ext')} (extract audio)"
        raise RuntimeError("No audio format available")

    limit = 720 if choice == "720" else (360 if choice == "360" else None)

    p = best_prog(limit)
    if p:
        return p["format_id"], f"progressive {p.get('ext')} {p.get('height', '?')}p"

    v = best_vo(limit) or best_vo(None)
    a = best_ao()
    if v and a:
        return f"{v['format_id']}+{a['format_id']}", f"mux {v.get('height','?')}p + {a.get('ext')}"

    p = best_prog()
    if p:
        return p["format_id"], f"progressive {p.get('ext')} {p.get('height','?')}p"

    any_id = (fmts[-1] or {}).get("format_id")
    if any_id:
        return any_id, "fallback single"
    raise RuntimeError("No suitable format found")


def pick_first_file(directory: str) -> Optional[Path]:
    files = sorted(Path(directory).glob("*"))
    return files[0] if files else None


# -------- multi-probe (web -> android) --------
def probe_info(url: str, out_dir: str) -> Tuple[Dict[str, Any], str]:
    """
    Спочатку пробуємо 'web' клієнт (найповніша матриця форматів),
    якщо падає — пробуємо 'android'. Повертаємо (info, used_client).
    """
    last_err: Exception | None = None
    for client in ("web", "android"):
        try:
            with yt_dlp.YoutubeDL(ydl_probe_opts(out_dir, client)) as ydl:
                info = ydl.extract_info(url, download=False)
            return info, client
        except Exception as e:
            last_err = e
            # йдемо на наступний клієнт
    raise last_err or RuntimeError("probe failed")


# -------------------- HANDLERS --------------------
@dp.message(CommandStart())
async def on_start(message: Message):
    text = (
        "Привіт! Я завантажу відео з <b>YouTube</b> або <b>TikTok</b>.\n"
        "Надішли посилання, а потім обери формат.\n\n"
        "Перед завантаженням я перевірю підписку на канал."
    )
    if CHANNEL_USERNAME or CHANNEL_ID:
        ch = CHANNEL_USERNAME or f"ID {CHANNEL_ID}"
        text += f"\n\nОбовʼязкова підписка на канал: <b>{ch}</b>"
    await message.answer(text)


@dp.message(F.text.regexp(SUPPORTED_URL_RE))
async def on_url(message: Message):
    m = SUPPORTED_URL_RE.search(message.text or "")
    if not m:
        return
    url = m.group(1)
    user_id = message.from_user.id

    pending[user_id] = PendingJob(url=url, user_id=user_id)

    if not await is_subscribed(user_id):
        hint = (
            "Будь ласка, спочатку підпишись на наш канал і повернись сюди.\n"
            "Після підписки просто натисни кнопку нижче ⤵️"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Перевірити підписку", callback_data="check:sub")
        if CHANNEL_USERNAME:
            kb.button(text="📣 Відкрити канал", url=f"https://t.me/{(CHANNEL_USERNAME or '').lstrip('@')}")
        kb.adjust(1)
        await message.answer(hint, reply_markup=kb.as_markup())
        return

    await message.answer(
        f"Отримав посилання:\n<code>{url}</code>\nОберіть формат завантаження:",
        reply_markup=build_main_menu(),
    )


@dp.callback_query(F.data == "check:sub")
async def on_check_sub(call: CallbackQuery):
    if await is_subscribed(call.from_user.id):
        await call.message.edit_text("Підписку підтверджено ✅\nОбери формат:", reply_markup=build_main_menu())
    else:
        await call.answer("Ще не бачу підписки 🙃", show_alert=True)


@dp.callback_query(F.data.startswith("fmt:"))
async def on_format_selected(call: CallbackQuery):
    user_id = call.from_user.id
    job = pending.get(user_id)
    if not job:
        await call.answer("Немає активного посилання. Надішли URL ще раз.", show_alert=True)
        return
    if not await is_subscribed(user_id):
        await call.answer("Спершу підпишись на канал 🙏", show_alert=True)
        return

    choice = call.data.split(":", 1)[1]
    url = job.url

    await call.message.edit_text("⏳ Готую завантаження...")

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1) ПРОБА (web -> android), повний ігнор конфігів
        try:
            info, client = probe_info(url, tmpdir)
        except Exception as e:
            await call.message.edit_text(f"❌ Не вдалося отримати інформацію про відео: <code>{e}</code>")
            return

        # 2) реальний підбір format_id
        try:
            fmt_string, note = pick_format_string(info, choice)
        except Exception as e:
            await call.message.edit_text(f"❌ Не вдалося підібрати формат: <code>{e}</code>")
            return

        await call.message.edit_text(f"⬇️ Завантажую ({note}, client={client})...")

        # 3) завантаження
        try:
            with yt_dlp.YoutubeDL(ydl_download_opts(tmpdir, fmt_string, choice, client)) as ydl:
                info2 = ydl.extract_info(url, download=True)
        except Exception as e:
            await call.message.edit_text(f"❌ Не вдалось завантажити: <code>{e}</code>")
            return

        file_path = pick_first_file(tmpdir)
        if not file_path or not file_path.exists():
            await call.message.edit_text("❌ Файл не знайдено після завантаження.")
            return

        size = file_path.stat().st_size
        if size > TELEGRAM_BOT_FILE_LIMIT_MB * 1024 * 1024:
            title = (info2 or {}).get("title") or file_path.stem
            await call.message.edit_text(
                "⚠️ Файл завеликий для надсилання через бота.\n"
                "Спробуй інший формат (нижча якість або аудіо).\n\n"
                f"<b>Назва:</b> {title}\n<b>Розмір:</b> {human_size(size)}\n"
                f"<b>Формат:</b> {note}"
            )
            return

        caption = f"Завантажено з: {url}\n<b>Формат:</b> {note}"
        try:
            if choice == "audio" or file_path.suffix.lower() in {".mp3", ".m4a"}:
                await bot.send_audio(
                    chat_id=call.message.chat.id,
                    audio=FSInputFile(str(file_path)),
                    caption=caption,
                    title=(info2 or {}).get("title"),
                    performer=(info2 or {}).get("uploader"),
                )
            else:
                await bot.send_video(
                    chat_id=call.message.chat.id,
                    video=FSInputFile(str(file_path)),
                    caption=caption,
                    supports_streaming=True,
                )
            await call.message.edit_text("✅ Готово! Надіслав файл вище.")
        except Exception as e:
            await call.message.edit_text(f"❌ Помилка під час надсилання файлу: <code>{e}</code>")

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp

# -------------------- ENV --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
CHANNEL_ID = os.getenv("CHANNEL_ID")
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in .env")

# -------------------- BOT CORE --------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

TELEGRAM_BOT_FILE_LIMIT_MB = 48
SUPPORTED_URL_RE = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be|tiktok\.com)/\S+)",
    re.IGNORECASE,
)

@dataclass
class PendingJob:
    url: str
    user_id: int

pending: dict[int, PendingJob] = {}


# -------------------- HELPERS --------------------
async def is_subscribed(user_id: int) -> bool:
    if not CHANNEL_USERNAME and not CHANNEL_ID:
        return True
    chat = CHANNEL_USERNAME or int(CHANNEL_ID)
    try:
        m = await bot.get_chat_member(chat_id=chat, user_id=user_id)
        return getattr(m, "status", None) in ("member", "administrator", "creator")
    except Exception:
        return False


def human_size(n: int) -> str:
    size, units = float(n), ["B", "KB", "MB", "GB"]
    for u in units:
        if size < 1024 or u == "GB":
            return f"{size:.1f}{u}"
        size /= 1024


def build_main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎬 Відео (краща якість)", callback_data="fmt:best")
    kb.button(text="🎥 Відео 720p", callback_data="fmt:720")
    kb.button(text="📱 Відео 360p", callback_data="fmt:360")
    kb.button(text="🎧 Аудіо (MP3)", callback_data="fmt:audio")
    kb.adjust(1)
    return kb.as_markup()


# -------- yt-dlp options (PROBE vs DOWNLOAD) --------
def _common_opts(out_dir: str, player_client: str) -> dict:
    opts = {
        "outtmpl": os.path.join(out_dir, "%(title).100s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        # повністю ігноруємо будь-які зовнішні конфіги/alias/ярлики
        "ignoreconfig": True,      # --ignore-config
        "config_locations": [],    # навіть якщо десь задані
        # обраний клієнт YouTube
        "extractor_args": {"youtube": {"player_client": [player_client]}},
        # пробуємо кілька разів на випадок мережевих флуктуацій
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 15,
    }
    if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
        opts["cookiefile"] = YT_COOKIES_FILE
    return opts


def ydl_probe_opts(out_dir: str, player_client: str) -> dict:
    return {
        **_common_opts(out_dir, player_client),
        "skip_download": True,  # ключове: тільки метадані
        # НІЯКИХ format/postprocessors/merge_output_format тут!
    }


def ydl_download_opts(out_dir: str, fmt_string: str, choice: str, player_client: str) -> dict:
    opts = {
        **_common_opts(out_dir, player_client),
        "format": fmt_string,
        "merge_output_format": "mp4",
        "postprocessors": [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},  # (так, саме preferedformat)
        ],
    }
    if choice == "audio":
        opts["postprocessors"].append(
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        )
    return opts


# -------- підбір реальних форматів --------
def _best_by(items: List[Dict[str, Any]], keys: List[str], reverse: bool = True):
    def k(f: Dict[str, Any]):
        vals = []
        for key in keys:
            v = f.get(key)
            if v is None:
                v = -1 if reverse else 10**12
            vals.append(v)
        return tuple(vals)
    return sorted(items, key=k, reverse=reverse)[0] if items else None


def pick_format_string(info: Dict[str, Any], choice: str) -> Tuple[str, str]:
    fmts: List[Dict[str, Any]] = info.get("formats") or []
    if not fmts:
        raise RuntimeError("No formats list from extractor")

    progressive = [f for f in fmts if f.get("vcodec") != "none" and f.get("acodec") != "none"]
    video_only  = [f for f in fmts if f.get("vcodec") != "none" and (f.get("acodec") in (None, "none"))]
    audio_only  = [f for f in fmts if f.get("acodec") != "none" and (f.get("vcodec") in (None, "none"))]

    def best_prog(max_h: Optional[int] = None):
        arr = progressive
        if max_h is not None:
            arr = [f for f in arr if (f.get("height") or 0) <= max_h or f.get("height") is None]
        mp4 = [f for f in arr if f.get("ext") == "mp4"]
        return _best_by(mp4, ["height", "tbr"]) or _best_by(arr, ["height", "tbr"])

    def best_vo(max_h: Optional[int] = None):
        arr = video_only
        if max_h is not None:
            arr = [f for f in arr if (f.get("height") or 0) <= max_h or f.get("height") is None]
        return _best_by(arr, ["height", "tbr"])

    def best_ao():
        m4a = [f for f in audio_only if f.get("ext") in ("m4a", "mp4")]
        return _best_by(m4a, ["abr", "tbr"]) or _best_by(audio_only, ["abr", "tbr"])

    if choice == "audio":
        a = best_ao()
        if a:
            return a["format_id"], f"audio-only {a.get('ext')} ~{a.get('abr') or a.get('tbr')}kbps"
        p = best_prog()
        if p:
            return p["format_id"], f"progressive {p.get('ext')} (extract audio)"
        raise RuntimeError("No audio format available")

    limit = 720 if choice == "720" else (360 if choice == "360" else None)

    p = best_prog(limit)
    if p:
        return p["format_id"], f"progressive {p.get('ext')} {p.get('height', '?')}p"

    v = best_vo(limit) or best_vo(None)
    a = best_ao()
    if v and a:
        return f"{v['format_id']}+{a['format_id']}", f"mux {v.get('height','?')}p + {a.get('ext')}"

    p = best_prog()
    if p:
        return p["format_id"], f"progressive {p.get('ext')} {p.get('height','?')}p"

    any_id = (fmts[-1] or {}).get("format_id")
    if any_id:
        return any_id, "fallback single"
    raise RuntimeError("No suitable format found")


def pick_first_file(directory: str) -> Optional[Path]:
    files = sorted(Path(directory).glob("*"))
    return files[0] if files else None


# -------- multi-probe (web -> android) --------
def probe_info(url: str, out_dir: str) -> Tuple[Dict[str, Any], str]:
    """
    Спочатку пробуємо 'web' клієнт (найповніша матриця форматів),
    якщо падає — пробуємо 'android'. Повертаємо (info, used_client).
    """
    last_err: Exception | None = None
    for client in ("web", "android"):
        try:
            with yt_dlp.YoutubeDL(ydl_probe_opts(out_dir, client)) as ydl:
                info = ydl.extract_info(url, download=False)
            return info, client
        except Exception as e:
            last_err = e
            # йдемо на наступний клієнт
    raise last_err or RuntimeError("probe failed")


# -------------------- HANDLERS --------------------
@dp.message(CommandStart())
async def on_start(message: Message):
    text = (
        "Привіт! Я завантажу відео з <b>YouTube</b> або <b>TikTok</b>.\n"
        "Надішли посилання, а потім обери формат.\n\n"
        "Перед завантаженням я перевірю підписку на канал."
    )
    if CHANNEL_USERNAME or CHANNEL_ID:
        ch = CHANNEL_USERNAME or f"ID {CHANNEL_ID}"
        text += f"\n\nОбовʼязкова підписка на канал: <b>{ch}</b>"
    await message.answer(text)


@dp.message(F.text.regexp(SUPPORTED_URL_RE))
async def on_url(message: Message):
    m = SUPPORTED_URL_RE.search(message.text or "")
    if not m:
        return
    url = m.group(1)
    user_id = message.from_user.id

    pending[user_id] = PendingJob(url=url, user_id=user_id)

    if not await is_subscribed(user_id):
        hint = (
            "Будь ласка, спочатку підпишись на наш канал і повернись сюди.\n"
            "Після підписки просто натисни кнопку нижче ⤵️"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Перевірити підписку", callback_data="check:sub")
        if CHANNEL_USERNAME:
            kb.button(text="📣 Відкрити канал", url=f"https://t.me/{(CHANNEL_USERNAME or '').lstrip('@')}")
        kb.adjust(1)
        await message.answer(hint, reply_markup=kb.as_markup())
        return

    await message.answer(
        f"Отримав посилання:\n<code>{url}</code>\nОберіть формат завантаження:",
        reply_markup=build_main_menu(),
    )


@dp.callback_query(F.data == "check:sub")
async def on_check_sub(call: CallbackQuery):
    if await is_subscribed(call.from_user.id):
        await call.message.edit_text("Підписку підтверджено ✅\nОбери формат:", reply_markup=build_main_menu())
    else:
        await call.answer("Ще не бачу підписки 🙃", show_alert=True)


@dp.callback_query(F.data.startswith("fmt:"))
async def on_format_selected(call: CallbackQuery):
    user_id = call.from_user.id
    job = pending.get(user_id)
    if not job:
        await call.answer("Немає активного посилання. Надішли URL ще раз.", show_alert=True)
        return
    if not await is_subscribed(user_id):
        await call.answer("Спершу підпишись на канал 🙏", show_alert=True)
        return

    choice = call.data.split(":", 1)[1]
    url = job.url
    bot = "@YouTubevideoDownloaderNewbot"

    await call.message.edit_text("⏳ Готую завантаження...")

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1) ПРОБА (web -> android), повний ігнор конфігів
        try:
            info, client = probe_info(url, tmpdir)
        except Exception as e:
            await call.message.edit_text(f"❌ Не вдалося отримати інформацію про відео: <code>{e}</code>")
            return

        # 2) реальний підбір format_id
        try:
            fmt_string, note = pick_format_string(info, choice)
        except Exception as e:
            await call.message.edit_text(f"❌ Не вдалося підібрати формат: <code>{e}</code>")
            return

        await call.message.edit_text(f"⬇️ Завантажую ({note}, client={client})...")

        # 3) завантаження
        try:
            with yt_dlp.YoutubeDL(ydl_download_opts(tmpdir, fmt_string, choice, client)) as ydl:
                info2 = ydl.extract_info(url, download=True)
        except Exception as e:
            await call.message.edit_text(f"❌ Не вдалось завантажити: <code>{e}</code>")
            return

        file_path = pick_first_file(tmpdir)
        if not file_path or not file_path.exists():
            await call.message.edit_text("❌ Файл не знайдено після завантаження.")
            return

        size = file_path.stat().st_size
        if size > TELEGRAM_BOT_FILE_LIMIT_MB * 1024 * 1024:
            title = (info2 or {}).get("title") or file_path.stem
            await call.message.edit_text(
                "⚠️ Файл завеликий для надсилання через бота.\n"
                "Спробуй інший формат (нижча якість або аудіо).\n\n"
                f"<b>Назва:</b> {title}\n<b>Розмір:</b> {human_size(size)}\n"
                f"<b>Формат:</b> {note}"
            )
            return

        caption = f"Завантажено з: {bot}\n<b>Формат:</b> {note}"
        try:
            if choice == "audio" or file_path.suffix.lower() in {".mp3", ".m4a"}:
                await bot.send_audio(
                    chat_id=call.message.chat.id,
                    audio=FSInputFile(str(file_path)),
                    caption=caption,
                    title=(info2 or {}).get("title"),
                    performer=(info2 or {}).get("uploader"),
                )
            else:
                await bot.send_video(
                    chat_id=call.message.chat.id,
                    video=FSInputFile(str(file_path)),
                    caption=caption,
                    supports_streaming=True,
                )
            await call.message.edit_text("✅ Готово! Надіслав файл вище.")
        except Exception as e:
            await call.message.edit_text(f"❌ Помилка під час надсилання файлу: <code>{e}</code>")


@dp.message()
async def on_other(message: Message):
    await message.reply(
        "Надішли посилання на відео з YouTube або TikTok.\n"
        "Приклад: https://youtu.be/dQw4w9WgXcQ"
    )


# -------------------- ENTRYPOINT --------------------
async def main():
    print("Bot is running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

@dp.message()
async def on_other(message: Message):
    await message.reply(
        "Надішли посилання на відео з YouTube або TikTok.\n"
        "Приклад: https://youtu.be/dQw4w9WgXcQ"
    )


# -------------------- ENTRYPOINT --------------------
async def main():
    print("Bot is running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
