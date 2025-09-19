# YouTube & TikTok Downloader Bot 🎬

Телеграм-бот на **Python + aiogram 3 + yt-dlp**, який дозволяє:
- завантажувати відео з **YouTube** ;
- обирати формат (краща якість, 720p, 360p або тільки аудіо MP3);
- перевіряти підписку на канал перед завантаженням;
- автоматично очищати тимчасові файли після відправлення.

---

## 🚀 Запуск

### 1. Клонування репозиторію
```bash
git clone https://github.com/your-username/YouTubeDownloader.git
cd YouTubeDownloader
2. Встановлення залежностей
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip wheel
pip install -r requirements.txt
3. Налаштування .env
BOT_TOKEN=your_telegram_bot_token
CHANNEL_USERNAME=@your_channel   # або CHANNEL_ID=-1001234567890

4. Встановлення ffmpeg

Windows: choco install ffmpeg

Ubuntu/Debian: sudo apt-get install ffmpeg

MacOS: brew install ffmpeg

5. Запуск бота
python bot.py

📦 Вимоги

Python 3.10+

ffmpeg

Telegram Bot API токен

🛠 Використані бібліотеки

aiogram 3
 — асинхронний фреймворк для Telegram Bot API

yt-dlp
 — завантаження відео з YouTube/TikTok

python-dotenv
 — для роботи з .env