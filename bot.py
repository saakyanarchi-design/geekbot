import asyncio
import logging
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Optional

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import pytz

# --- Flask для Render ---
from flask import Flask
from threading import Thread

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")
SPREADSHEET_ID = "1VEr06vI-UqFxhby6lUx5b9-KxOH4FX-EZ8SvrZ6Fks0"

# --- Flask приложение для Render ---
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Bot is running!"

@app_flask.route('/health')
def health():
    return "OK", 200

def run_flask():
    """Запускаем Flask сервер"""
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# --- Инициализация Google Sheets ---
def init_google_sheets():
    """Инициализация клиента Google Sheets"""
    try:
        if not GOOGLE_CREDENTIALS_JSON:
            logger.error("❌ Не найдены GOOGLE_CREDENTIALS в переменных окружения!")
            return None

        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build('sheets', 'v4', credentials=creds)
        sheet = service.spreadsheets()
        logger.info("✅ Успешное подключение к Google Sheets")
        return sheet
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Google Sheets: {e}")
        return None

# --- Получение данных из таблицы ---
def get_sheet_data():
    """Получение данных из Google таблицы"""
    try:
        sheet = init_google_sheets()
        if not sheet:
            return None

        result = sheet.values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="'Срез данных по состоянию на 06.07.2026'!A:Z"
        ).execute()

        values = result.get('values', [])

        if not values:
            logger.warning("⚠️ Таблица пуста или данные не найдены")
            return None

        logger.info(f"✅ Получено {len(values)} строк данных")
        return values

    except Exception as e:
        logger.error(f"❌ Ошибка получения данных из таблицы: {e}")
        return None

# --- Обработчики команд ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    welcome_message = (
        f"👋 Привет, {user.first_name}!\n\n"
        f"🤖 Я бот для работы с отчётами ОТБ.\n\n"
        f"📋 Доступные команды:\n"
        f"• /report - получить текущий отчёт\n"
        f"• /start - показать это сообщение\n\n"
        f"💡 Просто отправьте /report, чтобы получить актуальные данные!"
    )
    await update.message.reply_text(welcome_message)

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /report"""
    await update.message.reply_text("📊 Загружаю данные из Google Sheets...")

    try:
        data = get_sheet_data()

        if not data:
            await update.message.reply_text(
                "❌ Не удалось получить данные из таблицы.\n"
                "Проверьте подключение к Google Sheets."
            )
            return

        report_text = (
            f"📊 Отчёт ОТБ\n"
            f"📅 Дата: {datetime.now(pytz.timezone('Europe/Moscow')).strftime('%d.%m.%Y %H:%M')}\n\n"
            f"✅ Данные получены\n"
            f"📈 Всего записей: {len(data) - 1} (без заголовка)\n\n"
            f"⚙️ Расширенный функционал в разработке..."
        )

        await update.message.reply_text(report_text)

    except Exception as e:
        logger.error(f"❌ Ошибка в команде /report: {e}")
        await update.message.reply_text(f"❌ Произошла ошибка: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    if query.data == "refresh":
        await query.edit_message_text("🔄 Обновляю данные...")
        data = get_sheet_data()
        if data:
            await query.edit_message_text(
                f"✅ Данные обновлены!\n"
                f"📊 Всего записей: {len(data) - 1}"
            )
        else:
            await query.edit_message_text("❌ Не удалось обновить данные")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик обычных сообщений"""
    await update.message.reply_text(
        "🤖 Я вас не понял.\n"
        "Используйте команды:\n"
        "• /start - приветствие\n"
        "• /report - получить отчёт"
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"❌ Ошибка: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ Произошла ошибка. Пожалуйста, попробуйте позже."
        )

# --- Создание приложения бота ---
def create_application():
    """Создание и настройка приложения бота"""
    if not BOT_TOKEN:
        logger.error("❌ Не указан BOT_TOKEN в переменных окружения!")
        return None

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    return application

# --- Главная асинхронная функция ---
async def main():
    """Основная асинхронная функция запуска"""
    logger.info("🚀 Запуск бота...")

    # 1. Запускаем Flask в отдельном потоке (через asyncio.to_thread)
    flask_task = asyncio.to_thread(run_flask)
    logger.info(f"🌐 Flask сервер запущен на порту {os.environ.get('PORT', 8080)}")

    # 2. Создаем приложение Telegram бота
    application = create_application()

    if not application:
        logger.error("❌ Не удалось создать приложение бота")
        return

    logger.info("✅ Бот успешно создан")
    logger.info("🤖 Бот запущен и готов к работе!")

    # 3. Запускаем бота
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    # Держим бота активным
    try:
        # Бесконечное ожидание (бот работает, пока не остановят)
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Бот остановлен")
        await application.stop()

if __name__ == "__main__":
    # Используем asyncio.run для корректной работы event loop
    asyncio.run(main())
