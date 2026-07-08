import os
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Определяем временную зону ---
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# --- Загружаем переменные окружения ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = int(os.getenv('CHAT_ID')) if os.getenv('CHAT_ID') else None
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
RANGE_NAME = os.getenv('RANGE_NAME', 'A:Z')
PORT = int(os.getenv('PORT', 8080))

# --- Собираем словарь для Google Credentials ---
creds_dict = {
    "type": os.getenv("TYPE"),
    "project_id": os.getenv("PROJECT_ID"),
    "private_key_id": os.getenv("PRIVATE_KEY_ID"),
    "private_key": os.getenv("PRIVATE_KEY").replace("\\n", "\n"),
    "client_email": os.getenv("CLIENT_EMAIL"),
    "client_id": os.getenv("CLIENT_ID"),
    "auth_uri": os.getenv("AUTH_URI"),
    "token_uri": os.getenv("TOKEN_URI"),
    "auth_provider_x509_cert_url": os.getenv("AUTH_PROVIDER_CERT_URL"),
    "client_x509_cert_url": os.getenv("CLIENT_CERT_URL"),
}

# ========================= ВЕБ-СЕРВЕР ДЛЯ RENDER =========================
async def handle_health(request):
    """Обработчик для проверки здоровья сервиса Render."""
    return web.Response(text="GeekBot is running!")

async def start_web_server():
    """Запускает минимальный веб-сервер для Health Check Render."""
    app = web.Application()
    app.router.add_get('/', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {PORT}")

# ========================= ФУНКЦИЯ ДЛЯ ТАБЛИЦЫ =========================
def get_sheet_data():
    """Возвращает все значения из таблицы Google Sheets."""
    try:
        creds = Credentials.from_service_account_info(creds_dict)
        service = build('sheets', 'v4', credentials=creds)
        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE_NAME).execute()
        values = result.get('values', [])
        return values
    except Exception as e:
        logger.error(f"Ошибка при получении данных из таблицы: {e}")
        return None

# ========================= ФУНКЦИЯ ОТПРАВКИ =========================
async def send_message(text: str, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет сообщение в заданный чат."""
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text=text)
        logger.info(f"Сообщение отправлено в чат {CHAT_ID}")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

# ========================= КОМАНДЫ =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я GeekBot. Бот запущен и работает!")

async def get_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет данные из таблицы по запросу /get_data."""
    data = get_sheet_data()
    if data:
        lines = []
        for row in data[:5]:  # Показываем первые 5 строк
            lines.append(" | ".join(row))
        msg = "Данные из таблицы (первые 5 строк):\n" + "\n".join(lines)
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("Не удалось получить данные из таблицы.")

# ========================= ПЛАНИРОВЩИК =========================
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Проверка напоминаний (заглушка)."""
    data = get_sheet_data()
    if data:
        # Здесь можно добавить логику напоминаний
        pass

# ========================= ГЛАВНАЯ ФУНКЦИЯ =========================
async def main():
    """Основная асинхронная функция запуска бота."""
    try:
        # Сначала запускаем веб-сервер для Health Check Render
        await start_web_server()

        # Создаем приложение Telegram бота
        app = Application.builder().token(BOT_TOKEN).build()

        # Регистрируем команды
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("get_data", get_data))

        # Запускаем планировщик
        scheduler = AsyncIOScheduler(timezone=str(MOSCOW_TZ))
        scheduler.add_job(check_reminders, 'interval', minutes=5, args=[app])
        scheduler.start()

        logger.info("Бот запущен и готов к работе!")

        # Запускаем бота через polling
        await app.initialize()
        await app.start()
        await app.updater.start_polling()

        # Держим бота запущенным
        await asyncio.Event().wait()

    except Exception as e:
        logger.error(f"Критическая ошибка при запуске бота: {e}")
        raise

if __name__ == '__main__':
    asyncio.run(main())
