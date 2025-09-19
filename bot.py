import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

import yt_dlp

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")  # e.g. @my_channel
CHANNEL_ID = os.getenv("CHANNEL_ID")  # optional numeric id as string

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in .env")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher()

# ---- Config ----
TELEGRAM_BOT_FILE_LIMIT_MB = 48  # обмежимося ~48МБ, щоб уникати відхилення
SUPPORTED_URL_RE = re.compile(
    r"(https?://(www\.)?(youtube\.com|youtu\.be|tiktok\.com)/[^\s]+)",
    re.IGNORECASE
)

# ---- Models ----
@dataclass
class PendingJob:
    url: str
    user_id: int


# Простеньке сховище очікуючих завантажень (в пам'яті)
pending: dict[int, PendingJob] = {}  # key: user_id


# ---- Utils ----
async def is_subscribed(user_id: int) -> bool:
    """
    Перевіряє, чи користувач підписаний на канал.
    Використовує або CHANNEL_USERNAME, або CHANNEL_ID.
    """
    if not CHANNEL_USERNAME and not CHANNEL_ID:
        # якщо канал не задано — вважатимемо підписку не потрібною
        return True

    chat = CHANNEL_USERNAME or int(CHANNEL_ID)  # str @name або int id

    try:
        member = await bot.get_chat_member(chat_id=chat, user_id=user_id)
        status = getattr(member, "status", None)
        # subscribed if member, administrator, creator
        return status in ("member", "administrator", "creator")
    except Exception:
        # Якщо бот не має прав або канал приватний — не пройде.
        return False


def human_size(bytes_count: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(bytes_count)
    for u in units:
        if size < 1024 or u == "GB":
            return f"{size:.1f}{u}"
        size /= 1024


def build_main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎬 Відео (краща якість)", callback_data="fmt:best")
    kb.button(text="🎥 Відео 720p (MP4)", callback_data="fmt:720")
    kb.button(text="📱 Відео 360p (MP4)", callback_data="fmt:360")
    kb.button(text="🎧 Аудіо (MP3)", callback_data="fmt:audio")
    kb.adjust(1)
    return kb.as_markup()


def ydl_opts_for_choice(choice: str, out_dir: str) -> dict:
    common = {
        "outtmpl": os.path.join(out_dir, "%(title).100s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }
    if choice == "best":
        # Найкраща сумісна з телеграмом контейнеризація часто mp4/mkv
        return {
            **common,
            "format": "bv*+ba/b",  # bestvideo+audio, fallback best
            "merge_output_format": "mp4",
        }
    if choice == "720":
        # спроба взяти mp4 720p
        return {
            **common,
            "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
            "merge_output_format": "mp4",
        }
    if choice == "360":
        return {
            **common,
            "format": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best",
            "merge_output_format": "mp4",
        }
    if choice == "audio":
        return {
            **common,
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
        }
    # fallback
    return common


def pick_first_file(directory: str) -> Path | None:
    files = sorted(Path(directory).glob("*"))
    return files[0] if files else None


# ---- Handlers ----
@dp.message(CommandStart())
async def on_start(message: Message):
    text = (
        "Привіт! Я завантажу відео з <b>YouTube</b>.\n"
        "Надішли посилання, а потім обери формат.\n\n"
        "Перед завантаженням я перевірю підписку на канал."
    )
    if CHANNEL_USERNAME or CHANNEL_ID:
        ch_display = CHANNEL_USERNAME or f"ID {CHANNEL_ID}"
        text += f"\n\nОбовʼязкова підписка на канал: <b>{ch_display}</b>"
    await message.answer(text)


@dp.message(F.text.regexp(SUPPORTED_URL_RE))
async def on_url(message: Message):
    url = SUPPORTED_URL_RE.search(message.text).group(1)
    user_id = message.from_user.id

    # Зберігаємо запит
    pending[user_id] = PendingJob(url=url, user_id=user_id)

    # Перевірка підписки
    if not await is_subscribed(user_id):
        subscribe_hint = (
            "Будь ласка, спочатку підпишись на наш канал і повернись сюди.\n"
            "Після підписки просто натисни будь-яку кнопку нижче ⤵️"
        )
        # Дамо кнопку «Перевірити знову»
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Перевірити підписку", callback_data="check:sub")
        if CHANNEL_USERNAME:
            kb.button(text="📣 Відкрити канал", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")
        kb.adjust(1)
        await message.answer(subscribe_hint, reply_markup=kb.as_markup())
        return

    await message.answer(
        f"Отримав посилання:\n<code>{url}</code>\nОберіть формат завантаження:",
        reply_markup=build_main_menu()
    )


@dp.callback_query(F.data == "check:sub")
async def on_check_sub(call: CallbackQuery):
    user_id = call.from_user.id
    ok = await is_subscribed(user_id)
    if ok:
        await call.message.edit_text(
            "Підписку підтверджено ✅\nОбери формат:",
            reply_markup=build_main_menu()
        )
    else:
        await call.answer("Ще не бачу підписки 🙃", show_alert=True)


@dp.callback_query(F.data.startswith("fmt:"))
async def on_format_selected(call: CallbackQuery):
    user_id = call.from_user.id
    job = pending.get(user_id)
    if not job:
        await call.answer("Немає активного посилання. Надішли URL ще раз.", show_alert=True)
        return

    # на випадок, якщо розписались після меню
    if not await is_subscribed(user_id):
        await call.answer("Спершу підпишись на канал 🙏", show_alert=True)
        return

    choice = call.data.split(":", 1)[1]
    url = job.url
    YouTubevideoDownloader="@YouTubevideoDownloaderNewbot"

    await call.message.edit_text("⏳ Завантажую... (це може зайняти трохи часу)")

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = ydl_opts_for_choice(choice, tmpdir)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            await call.message.edit_text(f"❌ Не вдалось завантажити: <code>{e}</code>")
            return

        file_path = pick_first_file(tmpdir)
        if not file_path or not file_path.exists():
            await call.message.edit_text("❌ Файл не знайдено після завантаження.")
            return

        size = file_path.stat().st_size
        if size > TELEGRAM_BOT_FILE_LIMIT_MB * 1024 * 1024:
            # Завеликий для надсилання ботом: запропонуємо тимчасове рішення
            title = info.get("title") or file_path.stem
            await call.message.edit_text(
                "⚠️ Файл вийшов завеликим для надсилання через бота.\n"
                "Спробуй інший формат (нижча якість або аудіо), або скористайся прямим посиланням.\n\n"
                f"<b>Назва:</b> {title}\n<b>Розмір:</b> {human_size(size)}"
            )
            return

        caption = f"Завантажено з: {YouTubevideoDownloader}"

        try:
            if choice == "audio" or file_path.suffix.lower() in {".mp3", ".m4a"}:
                await bot.send_audio(
                    chat_id=call.message.chat.id,
                    audio=FSInputFile(str(file_path)),   # <-- тут
                    caption=caption,
                    title=info.get("title"),
                    performer=info.get("uploader")
                )
            else:
                await bot.send_video(
                    chat_id=call.message.chat.id,
                    video=FSInputFile(str(file_path)),   # <-- і тут
                    caption=caption,
                    supports_streaming=True
                )

            await call.message.edit_text("✅ Готово! Надіслав файл вище.")
        except Exception as e:
            await call.message.edit_text(f"❌ Помилка під час надсилання файлу: <code>{e}</code>")


@dp.message()
async def on_other(message: Message):
    await message.reply(
        "Надішли мені посилання на відео з YouTube \n"
        "Приклад: https://youtu.be/dQw4w9WgXcQ"
    )


async def main():
    print("Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

