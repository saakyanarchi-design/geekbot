import threading
import asyncio
import datetime
import logging
import os
import sys
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import List, Dict, Optional

import gspread
from google.oauth2 import service_account

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------------------------------------
# ЛОГИРОВАНИЕ
# ---------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ---------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "-1003813207765"))
SPREADSHEET_NAME = "Отчёты/ Срезы ОТБ Июль"
REF_SHEET_NAME = "СПРАВОЧНИКИ"
SCHEDULE_SHEET_NAME = "График"

# ---------------------------------------------------------
# GOOGLE SHEETS - ПОЛНОСТЬЮ ИСПРАВЛЕНО
# ---------------------------------------------------------

def init_google_sheets():
    try:
        # Проверяем, есть ли JSON-строка в GOOGLE_CREDENTIALS
        creds_json = os.getenv("GOOGLE_CREDENTIALS")

        if creds_json and creds_json.strip().startswith("{"):
            # Парсим JSON строку
            creds_dict = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(creds_dict)
        else:
            # Собираем credentials из отдельных переменных
            creds_dict = {
                "type": "service_account",
                "project_id": os.getenv("PROJECT_ID"),
                "private_key_id": os.getenv("PRIVATE_KEY_ID"),
                "private_key": os.getenv("PRIVATE_KEY", "").replace("\\n", "\n"),
                "client_email": os.getenv("CLIENT_EMAIL"),
                "client_id": os.getenv("CLIENT_ID"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_x509_cert_url": os.getenv("CLIENT_CERT_URL")
            }
            creds = service_account.Credentials.from_service_account_info(creds_dict)

        client = gspread.authorize(creds)

        spreadsheet_id = os.getenv("SPREADSHEET_ID")
        if spreadsheet_id:
            logger.info(f"Подключено к таблице по ID: {spreadsheet_id[:20]}...")
            return client.open_by_key(spreadsheet_id)

        logger.info(f"Подключено к таблице по имени: {SPREADSHEET_NAME}")
        return client.open(SPREADSHEET_NAME)

    except Exception as e:
        logger.error(f"Критическая ошибка Google Sheets: {e}", exc_info=True)
        return None

# Глобальный клиент
client = None

def get_spreadsheet():
    global client
    if client is None:
        logger.info("Инициализируем Google Sheets клиент...")
        client = init_google_sheets()
    if client is None:
        logger.error("❌ Клиент не инициализирован")
        return None
    try:
        return client.open(SPREADSHEET_NAME)
    except Exception as e:
        logger.error(f"Не могу открыть таблицу '{SPREADSHEET_NAME}': {e}")
        return None

# ---------------------------------------------------------
# ПОЛУЧЕНИЕ МЕНЕДЖЕРОВ И ДАННЫХ
# ---------------------------------------------------------

def get_active_managers_for_today() -> List[Dict[str, str]]:
    try:
        spreadsheet = get_spreadsheet()
        if not spreadsheet:
            return []

        ref_sheet = spreadsheet.worksheet(REF_SHEET_NAME)
        ref_data = ref_sheet.get_all_values()

        candidates = []
        for row in ref_data[1:]:
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                candidates.append({
                    "full_name": row[0].strip(),
                    "username": row[1].strip().replace("@", "")
                })

        if not candidates:
            logger.warning("Нет кандидатов в справочнике")
            return []

        schedule_sheet = spreadsheet.worksheet(SCHEDULE_SHEET_NAME)
        schedule_data = schedule_sheet.get_all_values()
        if not schedule_data:
            logger.warning("График пуст")
            return []

        today = datetime.datetime.now()
        today_str = str(today.day)

        header = schedule_data[0]
        col_idx = -1
        for i, val in enumerate(header):
            if str(val).strip() == today_str:
                col_idx = i
                break

        if col_idx == -1:
            logger.warning(f"Колонка даты {today_str} не найдена в графике")
            return []

        schedule_map = {}
        for row in schedule_data[1:]:
            if len(row) > col_idx and row[0].strip():
                name = row[0].strip()
                val = str(row[col_idx]).strip().lower()
                is_working = val in ["true", "1", "да", "✓", "✔", "yes", "+", "работает"]
                schedule_map[name] = is_working

        active = []
        for cand in candidates:
            if schedule_map.get(cand["full_name"], False):
                active.append(cand)

        logger.info(f"Активных менеджеров сегодня: {len(active)}")
        return active
    except Exception as e:
        logger.error(f"Ошибка get_active_managers: {e}", exc_info=True)
        return []

def get_manager_day_data(full_name: str) -> Optional[Dict[str, str]]:
    try:
        spreadsheet = get_spreadsheet()
        if not spreadsheet:
            return None

        now = datetime.datetime.now()
        sheet_names_to_try = [
            str(now.day),
            f"{now.day:02d}",
            str(now.day) + " " + now.strftime("%B")[:3],
            f"{now.day}.{now.month:02d}"
        ]

        day_sheet = None
        for name in sheet_names_to_try:
            try:
                day_sheet = spreadsheet.worksheet(name)
                logger.info(f"Найден лист: {name}")
                break
            except gspread.exceptions.WorksheetNotFound:
                continue

        if not day_sheet:
            for ws in spreadsheet.worksheets():
                ws_title = ws.title.strip()
                if ws_title == str(now.day) or ws_title == f"{now.day:02d}":
                    day_sheet = ws
                    break
            if not day_sheet:
                logger.warning(f"Лист для {now.day} не найден")
                return None

        all_data = day_sheet.get_all_values()
        if not all_data:
            return None

        row_num = None
        for i, row in enumerate(all_data):
            if row and row[0].strip().lower() == full_name.lower():
                row_num = i
                break

        if row_num is None:
            logger.warning(f"'{full_name}' не найден на листе {day_sheet.title}")
            return None

        def safe(row, col, default="0"):
            if col < len(row):
                v = row[col].strip()
                return v if v else default
            return default

        target = all_data[row_num]
        return {
            "full_name": full_name,
            "leads": safe(target, 1),
            "calls": safe(target, 2),
            "mailing": safe(target, 3),
            "vk_requests": safe(target, 4),
            "vk_checks": safe(target, 5),
            "deadlines": safe(target, 6),
            "invoices": safe(target, 7),
            "payments_count": safe(target, 8),
            "payments_sum": safe(target, 9),
            "cvr": safe(target, 10),
            "non_quality": safe(target, 11)
        }
    except Exception as e:
        logger.error(f"get_manager_day_data error: {e}", exc_info=True)
        return None

def format_manager_report(data: Dict[str, str]) -> str:
    return (
        f"📊 <b>ОТЧЕТ ПРИНЯТ</b>\n"
        f"──────────────────────\n"
        f"👤 <b>Менеджер:</b> {data['full_name']}\n"
        f"📅 <b>Дата:</b> {datetime.datetime.now().strftime('%d.%m.%Y')}\n"
        f"──────────────────────\n"
        f"🔹 <b>Лиды:</b> {data['leads']}\n"
        f"🔹 <b>Звонки:</b> {data['calls']}\n"
        f"🔹 <b>Рассылка:</b> {data['mailing']}\n"
        f"🔹 <b>ВК запросы:</b> {data['vk_requests']}\n"
        f"🔹 <b>ВК проверки:</b> {data['vk_checks']}\n"
        f"🔹 <b>Дедлайны:</b> {data['deadlines']}\n"
        f"🔹 <b>Счета:</b> {data['invoices']}\n"
        f"💰 <b>Оплаты:</b> {data['payments_count']} шт. на сумму {data['payments_sum']}₽\n"
        f"📈 <b>CVR:</b> {data['cvr']}%\n"
        f"🔹 <b>Некачественные:</b> {data['non_quality']}\n"
        f"──────────────────────"
    )

# ---------------------------------------------------------
# ХЕНДЛЕРЫ
# ---------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сдать отчёт", callback_data="submit_report")]
    ])
    await update.message.reply_text(
        "👋 Привет! Я бот для контроля отчётов ОТБ.\n"
        "Нажмите кнопку ниже, чтобы сдать отчёт.",
        reply_markup=kb
    )

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сдать отчёт", callback_data="submit_report")]
    ])
    await update.message.reply_text("📊 Нажмите кнопку, чтобы сдать отчёт:", reply_markup=kb)

async def process_callback_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    username = user.username

    if not username:
        await query.edit_message_text(
            "❌ <b>Ошибка!</b>\n\nУ вас не заполнен Username в Telegram.\n"
            "Пожалуйста, установите его в настройках: Настройки → Имя пользователя",
            parse_mode='HTML'
        )
        return

    spreadsheet = get_spreadsheet()
    if not spreadsheet:
        await query.edit_message_text(
            "❌ <b>Ошибка подключения к таблице!</b>\n\nПодробнее в логах Render.\n"
            "Пожалуйста, попробуйте позже.",
            parse_mode='HTML'
        )
        return

    try:
        ref_sheet = spreadsheet.worksheet(REF_SHEET_NAME)
        ref_data = ref_sheet.get_all_values()

        full_name = None
        for row in ref_data[1:]:
            if len(row) >= 2:
                ref_username = row[1].strip().replace("@", "").lower()
                if ref_username == username.lower():
                    full_name = row[0].strip()
                    break

        if not full_name:
            await query.edit_message_text(
                f"❌ <b>Вы не найдены в списке менеджеров!</b>\n\n"
                f"Ваш username: @{username}\n"
                f"Обратитесь к администратору, чтобы вас добавили в таблицу.",
                parse_mode='HTML'
            )
            return

        active_managers = get_active_managers_for_today()
        is_on_shift = any(m["full_name"] == full_name for m in active_managers)

        if not is_on_shift:
            await query.edit_message_text(
                f"❌ <b>Вы не на смене по графику!</b>\n\n"
                f"Сегодня ({datetime.datetime.now().strftime('%d.%m.%Y')}) "
                f"вас нет в графике работы.",
                parse_mode='HTML'
            )
            return

        manager_data = get_manager_day_data(full_name)

        if not manager_data:
            await query.edit_message_text(
                f"❌ <b>Ошибка получения данных!</b>\n\n"
                f"Ваши данные не найдены на листе текущего дня. "
                f"Возможно, вы не заполнили таблицу.",
                parse_mode='HTML'
            )
            return

        report_text = format_manager_report(manager_data)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=report_text,
            parse_mode='HTML'
        )

        current_time = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
        await query.edit_message_text(
            f"✅ <b>Отчёт успешно отправлен!</b>\n\n"
            f"📅 {current_time}",
            parse_mode='HTML'
        )

    except Exception as e:
        logger.error(f"Ошибка при отправке отчёта: {e}", exc_info=True)
        await query.edit_message_text(
            f"❌ <b>Произошла ошибка!</b>\n\n"
            f"Ошибка: {e}\n"
            f"Пожалуйста, попробуйте позже или сообщите администратору.",
            parse_mode='HTML'
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Произошла ошибка. Пожалуйста, попробуйте позже."
            )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение об ошибке: {e}")

# ---------------------------------------------------------
# НАПОМИНАНИЕ
# ---------------------------------------------------------

async def send_reminder(bot):
    try:
        managers = get_active_managers_for_today()
        if not managers:
            logger.info("Нет активных менеджеров — напоминание не отправлено")
            return

        mentions = "\n".join([f"• {m['full_name']} (@{m['username']})" for m in managers])
        mention_tags = " ".join([f"@{m['username']}" for m in managers])

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Сдать отчёт", callback_data="submit_report")]
        ])

        msg = (
            f"🔔 <b>Время сдавать отчёт!</b>\n\n"
            f"Сегодня на смене:\n{mentions}\n\n"
            f"{mention_tags}\n\n"
            f"Пожалуйста, заполните таблицу и нажмите кнопку ниже 👇"
        )

        await bot.send_message(
            chat_id=CHAT_ID,
            text=msg,
            parse_mode='HTML',
            reply_markup=kb
        )
        logger.info(f"Напоминание отправлено. Менеджеров: {len(managers)}")
    except Exception as e:
        logger.error(f"Ошибка в send_reminder: {e}")

# ---------------------------------------------------------
# ЗАПУСК - ЕДИНСТВЕННАЯ ТОЧКА ЗАПУСКА
# ---------------------------------------------------------

async def main_async():
    logger.info("🚀 Запуск бота...")

    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не найден!")
        sys.exit(1)

    # Инициализируем Google Sheets
    global client
    client = init_google_sheets()

    # Создаём приложение
    application = Application.builder().token(BOT_TOKEN).build()

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("report", cmd_report))
    application.add_handler(CallbackQueryHandler(process_callback_submit, pattern="submit_report"))
    application.add_error_handler(error_handler)

    # Планировщик (МСК)
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_reminder, 'cron', hour=14, minute=50, args=[application.bot])
    scheduler.add_job(send_reminder, 'cron', hour=19, minute=30, args=[application.bot])
    scheduler.start()
    logger.info("✅ Планировщик запущен (14:50, 19:30 МСК)")

    # ЕДИНСТВЕННЫЙ запуск: удаляем webhook, инициализируем, стартуем
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook удалён, все ожидающие обновления сброшены")
    except Exception as e:
        logger.warning(f"Не удалось удалить webhook: {e}")

    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=30
    )

    logger.info("🤖 Бот успешно запущен!")

    # Держим процесс живым
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("⏹ Бот остановлен")
        await application.shutdown()

def main():
    """Микро-сервер для Render + запуск бота"""
    def run_server():
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

        port = int(os.getenv("PORT", 10000))
        server = HTTPServer(('0.0.0.0', port), Handler)
        logger.info(f"Health check server on port {port}")
        server.serve_forever()

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    asyncio.run(main_async())

if __name__ == "__main__":
    main()
