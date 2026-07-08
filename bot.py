import threading
import asyncio
import datetime
import logging
import os
import json
import gspread
from http.server import HTTPServer, BaseHTTPRequestHandler
from google.oauth2 import service_account
from typing import List, Dict, Optional
from google.oauth2 import service_account
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---------------------------------------------------------
# НАСТРОЙКИ ЛОГИРОВАНИЯ
# ---------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# НАСТРОЙКИ БОТА
# ---------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "-1003813207765"))
SPREADSHEET_NAME = "Отчёты/ Срезы ОТБ Июль"
REF_SHEET_NAME = "СПРАВОЧНИКИ"
SCHEDULE_SHEET_NAME = "График"

# ---------------------------------------------------------
# ПОДГОТОВКА GOOGLE SHEETS
# ---------------------------------------------------------
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
client = None

def init_google_sheets():
    """Инициализация Google Sheets API из переменных окружения"""
    try:
        SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
        creds_dict = {
            "type": "service_account",
            "project_id": os.getenv("PROJECT_ID"),
            "private_key_id": os.getenv("PRIVATE_KEY_ID"),
            "private_key": os.getenv("PRIVATE_KEY").replace("\\n", "\n") if os.getenv("PRIVATE_KEY") else None,
            "client_email": os.getenv("CLIENT_EMAIL"),
            "client_id": os.getenv("CLIENT_ID"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": os.getenv("CLIENT_X509_CERT_URL")
        }

        # Проверка обязательных полей
        required_fields = ["private_key", "client_email", "client_id"]
        missing = [f for f in required_fields if not creds_dict[f]]
        if missing:
            logger.error(f"Missing required credentials: {', '.join(missing)}")
            return None

        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=SCOPES)
        client = gspread.authorize(creds)

        spreadsheet_id = os.getenv("SPREADSHEET_ID")
        if not spreadsheet_id:
            logger.error("SPREADSHEET_ID not set")
            return None

        sheet = client.open_by_key(spreadsheet_id)
        logger.info(f"Connected to spreadsheet: {sheet.title}")
        return sheet
    except Exception as e:
        logger.error(f"Google Sheets init error: {e}", exc_info=True)
        return None

def get_spreadsheet():
    """Получение таблицы по имени"""
    if not client:
        init_google_sheets()
    if not client:
        return None
    try:
        return client.open(SPREADSHEET_NAME)
    except gspread.exceptions.SpreadsheetNotFound:
        logger.error(f"❌ Не найдена таблица: {SPREADSHEET_NAME}")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка открытия таблицы: {e}")
        return None

# ---------------------------------------------------------
# ЛОГИКА ПОЛУЧЕНИЯ АКТИВНЫХ МЕНЕДЖЕРОВ
# ---------------------------------------------------------

def get_active_managers_for_today() -> List[Dict[str, str]]:
    """Получение списка активных менеджеров на сегодня по графику"""
    try:
        spreadsheet = get_spreadsheet()
        if not spreadsheet:
            return []

        # Читаем справочник
        ref_sheet = spreadsheet.worksheet(REF_SHEET_NAME)
        ref_data = ref_sheet.get_all_values()

        candidates = []
        for row in ref_data[1:]:  # Пропускаем заголовок
            if len(row) >= 2 and row[0] and row[1]:
                candidates.append({
                    "full_name": row[0].strip(),
                    "username": row[1].strip().replace("@", "")
                })

        if not candidates:
            logger.warning("Не найдено активных менеджеров в справочнике.")
            return []

        # Читаем график
        schedule_sheet = spreadsheet.worksheet(SCHEDULE_SHEET_NAME)
        schedule_data = schedule_sheet.get_all_values()
        if not schedule_data:
            logger.warning("Лист 'График' пуст")
            return []

        today_str = str(datetime.datetime.now().day)  # "8" вместо "08"
        header_row = schedule_data[0]
        target_col_index = -1

        for idx, val in enumerate(header_row):
            clean_val = str(val).strip()
            if clean_val == today_str:
                target_col_index = idx
                break

        if target_col_index == -1:
            logger.warning(f"Не найдена колонка с датой {today_str}")
            return []

        # Строим карту статусов
        schedule_map = {}
        for row in schedule_data[1:]:
            if len(row) > target_col_index:
                name = row[0].strip() if row[0] else ""
                status_cell = row[target_col_index] if target_col_index < len(row) else ""
                is_working = str(status_cell).strip().lower() in ["true", "1", "да", "✓", "✔"]
                schedule_map[name] = is_working

        # Фильтруем кандидатов
        active_managers = []
        for cand in candidates:
            if schedule_map.get(cand["full_name"], False):
                active_managers.append(cand)

        return active_managers

    except Exception as e:
        logger.error(f"Ошибка при получении списка менеджеров: {e}")
        return []

# ---------------------------------------------------------
# ПОЛУЧЕНИЕ ДАННЫХ МЕНЕДЖЕРА С ЛИСТА ДНЯ
# ---------------------------------------------------------

def get_manager_day_data(full_name: str) -> Optional[Dict[str, str]]:
    """Получение данных менеджера с листа текущего дня"""
    try:
        spreadsheet = get_spreadsheet()
        if not spreadsheet:
            return None

        # Определяем имя листа дня
        day_num = datetime.datetime.now().day  # Без ведущего нуля: "8", "15"
        day_sheet_name = str(day_num)

        # Пробуем открыть лист с числом, если нет - пробуем с нулем
        try:
            day_sheet = spreadsheet.worksheet(day_sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            # Пробуем с ведущим нулем
            day_sheet_name = f"{day_num:02d}"  # "08", "15"
            try:
                day_sheet = spreadsheet.worksheet(day_sheet_name)
            except gspread.exceptions.WorksheetNotFound:
                logger.warning(f"Не найден лист с датой: {day_num} или {day_num:02d}")
                return None

        # Получаем все данные с листа
        all_data = day_sheet.get_all_values()

        # Ищем строку менеджера (первая колонка - имя)
        row_num = None
        for idx, row in enumerate(all_data):
            if row and row[0].strip().lower() == full_name.lower():
                row_num = idx
                break

        if row_num is None:
            logger.warning(f"Менеджер {full_name} не найден на листе {day_sheet_name}")
            return None

        # Индексы столбцов (0-индексация):
        # B=1, C=2, D=3, E=4, F=5, G=6, H=7, I=8, J=9, K=10, L=11
        def safe_get(row_data, col_idx, default="0"):
            if col_idx < len(row_data):
                val = row_data[col_idx].strip()
                return val if val else default
            return default

        target_row = all_data[row_num]

        return {
            "full_name": full_name,
            "leads": safe_get(target_row, 1),     # B - Лиды
            "calls": safe_get(target_row, 2),     # C - Звонки
            "mailing": safe_get(target_row, 3),   # D - Рассылка
            "vk_requests": safe_get(target_row, 4),   # E - ВК запросы
            "vk_checks": safe_get(target_row, 5),     # F - ВК проверки
            "deadlines": safe_get(target_row, 6),     # G - Дедлайны
            "invoices": safe_get(target_row, 7),      # H - Счета
            "payments_count": safe_get(target_row, 8), # I - Кол-во оплат
            "payments_sum": safe_get(target_row, 9),   # J - Сумма оплат
            "cvr": safe_get(target_row, 10),           # K - CVR
            "non_quality": safe_get(target_row, 11)    # L - Некачественные
        }

    except Exception as e:
        logger.error(f"Ошибка получения данных менеджера: {e}")
        return None

def format_manager_report(data: Dict[str, str]) -> str:
    """Форматирование отчета менеджера для отправки"""
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
# ОБРАБОТЧИКИ КОМАНД
# ---------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /start с кнопкой отчета в ЛС"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сдать отчёт", callback_data="submit_report")]
    ])
    await update.message.reply_text(
        "👋 Привет! Я бот для контроля отчётов ОТБ.\n"
        "Нажмите кнопку ниже, чтобы сдать отчёт.",
        reply_markup=kb
    )

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /report - кнопка отчета"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сдать отчёт", callback_data="submit_report")]
    ])
    await update.message.reply_text("📊 Нажмите кнопку, чтобы сдать отчёт:", reply_markup=kb)

async def process_callback_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Сдать отчёт'"""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    username = user.username

    # Проверка username
    if not username:
        await query.edit_message_text(
            "❌ <b>Ошибка!</b>\n\nУ вас не заполнен Username в Telegram.\n"
            "Пожалуйста, установите его в настройках: Настройки → Имя пользователя",
            parse_mode='HTML'
        )
        return

    # Проверяем доступ к таблице
    spreadsheet = get_spreadsheet()
    if not spreadsheet:
        await query.edit_message_text(
            "❌ <b>Ошибка подключения к таблице!</b>\n"
            "Пожалуйста, попробуйте позже.",
            parse_mode='HTML'
        )
        return

    try:
        # Ищем менеджера по username в справочнике
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

        # Проверка графика работы
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

        # Получаем данные с листа дня
        manager_data = get_manager_day_data(full_name)

        if not manager_data:
            await query.edit_message_text(
                f"❌ <b>Ошибка получения данных!</b>\n\n"
                f"Ваши данные не найдены на листе текущего дня. "
                f"Возможно, вы не заполнили таблицу.",
                parse_mode='HTML'
            )
            return

        # Отправляем отчет в группу
        report_text = format_manager_report(manager_data)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=report_text,
            parse_mode='HTML'
        )

        # Подтверждение в ЛС
        current_time = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
        await query.edit_message_text(
            f"✅ <b>Отчёт успешно отправлен!</b>\n\n"
            f"📅 {current_time}",
            parse_mode='HTML'
        )

    except Exception as e:
        logger.error(f"Ошибка при отправке отчёта: {e}")
        await query.edit_message_text(
            f"❌ <b>Произошла ошибка!</b>\n\n"
            f"Пожалуйста, попробуйте позже или сообщите администратору.",
            parse_mode='HTML'
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {context.error}")
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Произошла ошибка. Пожалуйста, попробуйте позже."
            )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение об ошибке: {e}")

# ---------------------------------------------------------
# ПЛАНИРОВЩИК
# ---------------------------------------------------------

async def send_reminder(bot):
    """Отправка напоминания в группу с кнопкой"""
    try:
        managers = get_active_managers_for_today()
        if not managers:
            logger.info("Сегодня нет активных менеджеров, напоминание не отправлено")
            return

        # Создаем упоминания
        mentions = "\n".join([f"• {m['full_name']} (@{m['username']})" for m in managers])
        mention_tags = " ".join([f"@{m['username']}" for m in managers])

        # Кнопка для отчета
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
        logger.error(f"Ошибка в напоминалке: {e}")

# ---------------------------------------------------------
# ---------------------------------------------------------
# ЗАПУСК БОТА
# ---------------------------------------------------------

async def main_async():
    """Асинхронная главная функция запуска"""
    logger.info("🚀 Запуск бота...")

    # Проверяем токен
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не найден в переменных окружения!")
        return

    # Инициализируем Google Sheets
    init_google_sheets()

    # Создаём приложение
    application = Application.builder().token(BOT_TOKEN).build()

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("report", cmd_report))
    application.add_handler(CallbackQueryHandler(process_callback_submit, pattern="submit_report"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_report_responses))
    application.add_error_handler(error_handler)

    # Настраиваем планировщик (московское время)
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        send_reminder,
        'cron',
        hour=14,
        minute=50,
        args=[application.bot],
        id="reminder_14_50"
    )
    scheduler.add_job(
        send_reminder,
        'cron',
        hour=19,
        minute=30,
        args=[application.bot],
        id="reminder_19_30"
    )
    scheduler.start()
    logger.info("✅ Планировщик запущен (напоминания в 14:50 и 19:30)")

    # Запускаем бота
    logger.info("🤖 Бот запущен и готов к работе!")

    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

    # Держим бота запущенным
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("⏹ Бот остановлен")
        await application.shutdown()

    def run_dummy_server():
        class HealthCheckHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args): return # Отключает лишний спам в логах
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")

        server = HTTPServer(('0.0.0.0', int(os.getenv("PORT", 10000))), HealthCheckHandler)
        server.serve_forever()


if __name__ == "__main__":
    main()
