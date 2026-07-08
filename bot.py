import os
import asyncio
import logging
from datetime import datetime, timedelta
import pytz
import requests
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
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
PORT = int(os.getenv('PORT', 8080))

# Названия листов (точь-в-точь как у вас)
REF_SHEET_NAME = "СПРАВОЧНИКИ"
SCHEDULE_SHEET_NAME = "График"

# --- Собираем словарь для Google Credentials из ENV ---
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
    return web.Response(text="GeekBot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {PORT}")

# ========================= РАБОТА С GOOGLE SHEETS =========================
def get_google_service():
    """Создает сервис Google Sheets."""
    try:
        creds = Credentials.from_service_account_info(creds_dict)
        service = build('sheets', 'v4', credentials=creds)
        return service
    except Exception as e:
        logger.error(f"Ошибка авторизации Google: {e}")
        return None

def get_sheet_data(sheet_name, range_name='A:Z'):
    """Получает данные с конкретного листа."""
    service = get_google_service()
    if not service:
        return None
    try:
        # Важно: указываем ИД таблицы и диапазон с именем листа
        full_range = f"'{sheet_name}'!{range_name}"
        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=full_range).execute()
        return result.get('values', [])
    except Exception as e:
        logger.error(f"Ошибка чтения листа {sheet_name}: {e}")
        return None

# ========================= ГЛАВНАЯ МАГИЯ: СПИСОК АКТИВНЫХ МЕНЕДЖЕРОВ =========================
def get_active_managers_for_today():
    """
    Основная логика:
    1. Берем СПРАВОЧНИКИ: ищем строки, где есть полное имя (столбец A) И никнейм (столбец B).
       Если никнейма нет — человек уволен/неактивен, игнорируем.
    2. Берем График: ищем колонку с сегодняшним ЧИСЛОМ.
    3. Возвращаем только тех, у кого стоит отметка 'TRUE' (или аналог) и есть никнейм.
    """
    try:
        # 1. Читаем справочник
        ref_data = get_sheet_data(REF_SHEET_NAME)
        if not ref_data or len(ref_data) < 2:
            logger.warning("Нет данных в справочнике")
            return []

        candidates = []
        # Пропускаем заголовок (индекс 0)
        for row in ref_data[1:]:
            # Проверяем, что есть и Имя (A) и Никнейм (B)
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                candidates.append({
                    "full_name": row[0].strip(),
                    "username": row[1].strip().replace("@", "") # Убираем @ если есть
                })

        if not candidates:
            logger.info("Нет кандидатов с заполненными никнеймами")
            return []

        # 2. Читаем график
        schedule_data = get_sheet_data(SCHEDULE_SHEET_NAME)
        if not schedule_data:
            return []

        # Ищем колонку с сегодняшним числом
        today_str = datetime.now(MOSCOW_TZ).strftime("%d")
        header_row = schedule_data[0]
        target_col_index = -1

        for idx, val in enumerate(header_row):
            clean_val = str(val).strip()
            if clean_val == today_str or clean_val == str(int(today_str)):
                target_col_index = idx
                break

        if target_col_index == -1:
            logger.warning(f"Не найдена колонка с датой {today_str} в графике.")
            return []

        # 3. Строим карту: Имя -> Статус (TRUE/FALSE)
        schedule_map = {}
        for row in schedule_data[1:]:
            if len(row) > target_col_index and row[0].strip():
                name = row[0].strip()
                status_cell = row[target_col_index] if target_col_index < len(row) else ""
                is_working = str(status_cell).strip().lower() in ["true", "1", "да", "✓", "+"]
                schedule_map[name] = is_working

        # 4. Финальный фильтр
        active_managers = []
        for cand in candidates:
            if schedule_map.get(cand["full_name"], False):
                active_managers.append(cand)

        logger.info(f"Найдено активных менеджеров с никнеймами: {len(active_managers)}")
        return active_managers

    except Exception as e:
        logger.error(f"Критическая ошибка в get_active_managers: {e}")
        return []

# ========================= КОМАНДЫ БОТА =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Стартовое сообщение с кнопкой."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Сдать отчёт", callback_data="submit_report")]
    ])
    await update.message.reply_text(
        "Привет! Я бот для контроля отчётов.\nНажмите кнопку ниже, чтобы сдать отчёт.",
        reply_markup=keyboard
    )

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /report — дублируем кнопку."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Сдать отчёт", callback_data="submit_report")]
    ])
    await update.message.reply_text("Ваш отчёт:", reply_markup=keyboard)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия на кнопку."""
    query = update.callback_query
    await query.answer()  # Убираем "часики"

    if query.data == "submit_report":
        user = query.from_user
        username = user.username

        if not username:
            await query.message.reply_text("⚠️ У вас не установлен username в Telegram. Свяжитесь с администратором.")
            return

        # 1. Получаем полное имя из справочника
        ref_data = get_sheet_data(REF_SHEET_NAME)
        full_name = None
        if ref_data:
            for row in ref_data[1:]:
                if len(row) >= 2 and row[1].strip().replace("@", "").lower() == username.lower():
                    full_name = row[0].strip()
                    break

        if not full_name:
            await query.message.reply_text(f"❌ @{username} не найден в справочнике или уволен.")
            return

        # 2. Проверяем график
        active_managers = get_active_managers_for_today()
        is_on_shift = any(m["full_name"] == full_name for m in active_managers)

        if not is_on_shift:
            await query.message.reply_text("⛔ Вы сегодня не на смене по графику!")
            return

        # 3. Отправка отчёта в группу
        now = datetime.now(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M')
        await context.bot.send_message(
            CHAT_ID,
            f"📋 Отчёт сдан!\n👤 Менеджер: {full_name} (@{username})\n🕒 Время: {now}"
        )
        await query.message.reply_text("✅ Отчёт успешно отправлен в группу!")
        logger.info(f"Отчёт от {full_name} (@{username}) отправлен.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка, кто сегодня на смене."""
    managers = get_active_managers_for_today()
    if not managers:
        await update.message.reply_text("Сегодня на смене никого нет.")
        return
    names = "\n".join([f"• {m['full_name']} (@{m['username']})" for m in managers])
    await update.message.reply_text(f"👥 Сейчас на смене:\n{names}")

# ========================= ПЛАНИРОВЩИК (НАПОМИНАЛКА) =========================
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача: утром напоминает, кто на смене."""
    try:
        managers = get_active_managers_for_today()
        if not managers:
            return
        names = "\n".join([f"• {m['full_name']} (@{m['username']})" for m in managers])
        msg = f"⏰ Доброе утро! Сегодня на смене:\n{names}\nНе забудьте сдать отчёты: /report"
        await context.bot.send_message(CHAT_ID, msg)
        logger.info("Утренняя напоминалка отправлена.")
    except Exception as e:
        logger.error(f"Ошибка в напоминалке: {e}")

# ========================= ГЛАВНАЯ ФУНКЦИЯ =========================
async def main():
    # 1. Запускаем заглушку для Render
    await start_web_server()

    # 2. Создаем приложение
    app = Application.builder().token(BOT_TOKEN).build()

    # 3. Регистрируем хендлеры
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^submit_report$"))

    # 4. Планировщик
    scheduler = AsyncIOScheduler(timezone=str(MOSCOW_TZ))
    # Будем слать напоминалку в 09:00 утра
    scheduler.add_job(send_reminder, 'cron', hour=9, minute=0, args=[app])
    scheduler.start()
    logger.info("Планировщик запущен.")

    # 5. Поллинг
    logger.info("Бот запущен и готов к работе!")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
