import os
import sys
import logging
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict
from http.server import HTTPServer, BaseHTTPRequestHandler

import gspread
from google.oauth2 import service_account

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
from aiohttp import web

# ---------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------
TZ_MOSCOW = ZoneInfo("Europe/Moscow")

def get_now_moscow() -> datetime:
    return datetime.now(TZ_MOSCOW)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ---------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

if not BOT_TOKEN:
    logger.error("❌ КРИТИЧЕСКАЯ ОШИБКА: BOT_TOKEN не найден!")
    sys.exit(1)

if CHAT_ID:
    try:
        CHAT_ID = int(CHAT_ID)
    except ValueError:
        logger.warning("⚠️ CHAT_ID не число, оставляем строку")

# ---------------------------------------------------------
# GOOGLE SHEETS
# ---------------------------------------------------------
_spreadsheet_cache = None
_gs_lock = asyncio.Lock()

def init_google_sheets() -> Optional[gspread.Spreadsheet]:
    try:
        if not SPREADSHEET_ID:
            logger.error("❌ SPREADSHEET_ID не задан!")
            return None

        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if not creds_json or not creds_json.strip().startswith("{"):
            logger.error("❌ GOOGLE_CREDENTIALS пуст или не JSON!")
            return None

        creds_dict = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.file"
            ]
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        logger.info(f"✅ Таблица подключена: {spreadsheet.title}")
        return spreadsheet

    except Exception as e:
        logger.error(f"❌ Ошибка Google Sheets: {e}", exc_info=True)
        return None

async def get_spreadsheet() -> Optional[gspread.Spreadsheet]:
    global _spreadsheet_cache
    async with _gs_lock:
        if _spreadsheet_cache is None:
            _spreadsheet_cache = init_google_sheets()
        return _spreadsheet_cache

# ---------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ---------------------------------------------------------
def normalize_name(name_str: str) -> str:
    if not name_str:
        return ""
    return str(name_str).replace(".", "").strip().lower()

def match_names(name_in_table: str, name_to_find: str) -> bool:
    t = normalize_name(name_in_table)
    f = normalize_name(name_to_find)
    if t == f:
        return True
    t_parts = t.split()
    f_parts = f.split()
    if not t_parts or not f_parts:
        return False
    return t_parts == f_parts

def get_cell_value(row: List[str], index: int, default: str = "0") -> str:
    if index < len(row):
        v = str(row[index]).strip()
        return v if v else default
    return default

# ---------------------------------------------------------
# ЛОГИКА ДАННЫХ
# ---------------------------------------------------------
async def get_active_managers_for_today() -> List[Dict[str, str]]:
    try:
        spreadsheet = await get_spreadsheet()
        if not spreadsheet:
            return []

        try:
            ref_sheet = spreadsheet.worksheet("СПРАВОЧНИКИ")
        except gspread.exceptions.WorksheetNotFound:
            logger.error("❌ Лист 'СПРАВОЧНИКИ' не найден!")
            return []

        ref_data = ref_sheet.get_all_values()
        if len(ref_data) < 2:
            return []

        candidates = []
        for row in ref_data[1:]:
            if len(row) >= 2:
                name_val = str(row[0]).strip()
                username_val = str(row[1]).strip().replace("@", "").lower()
                if name_val and username_val:
                    candidates.append({"full_name": name_val, "username": username_val})

        if not candidates:
            return []

        try:
            schedule_sheet = spreadsheet.worksheet("График")
        except gspread.exceptions.WorksheetNotFound:
            logger.error("❌ Лист 'График' не найден!")
            return []

        schedule_data = schedule_sheet.get_all_values()
        if not schedule_data:
            return []

        now = get_now_moscow()
        today_str = str(now.day)
        header = schedule_data[0] if schedule_data else []

        col_idx = -1
        for i, val in enumerate(header):
            if str(val).strip() == today_str:
                col_idx = i
                break

        if col_idx == -1:
            logger.warning(f"Колонка даты {today_str} не найдена")
            return []

        schedule_map = {}
        for row in schedule_data[1:]:
            if len(row) > col_idx:
                name = str(row[0]).strip()
                val = str(row[col_idx]).strip().lower()
                is_working = val in ["true", "1", "да", "✓", "✔", "yes", "+", "работает"]
                schedule_map[name] = is_working

        active = []
        for cand in candidates:
            found_name = None
            for sched_name, is_on in schedule_map.items():
                if match_names(sched_name, cand["full_name"]) and is_on:
                    found_name = sched_name
                    break
            if found_name:
                active.append(cand)

        logger.info(f"✅ Активных менеджеров сегодня: {len(active)}")
        return active
    except Exception as e:
        logger.error(f"❌ Ошибка get_active_managers: {e}", exc_info=True)
        return []

async def get_manager_day_data(full_name: str) -> Optional[Dict[str, str]]:
    try:
        spreadsheet = await get_spreadsheet()
        if not spreadsheet:
            return None

        now = get_now_moscow()
        day = now.day
        sheet_names_to_try = [
            str(day),
            f"{day:02d}",
            f"{day} {now.strftime('%B')[:3]}",
            f"{day}.{now.month:02d}"
        ]

        day_sheet = None
        for name in sheet_names_to_try:
            try:
                day_sheet = spreadsheet.worksheet(name)
                logger.info(f"📄 Найден лист: {name}")
                break
            except gspread.exceptions.WorksheetNotFound:
                continue

        if not day_sheet:
            return None

        all_data = day_sheet.get_all_values()
        if not all_data:
            return None

        row_num = None
        for i, row in enumerate(all_data):
            if row and len(row) > 0:
                if match_names(str(row[0]).strip(), full_name):
                    row_num = i
                    break

        if row_num is None:
            return None

        target = all_data[row_num]

        return {
            "full_name": full_name,
            "leads": get_cell_value(target, 1),
            "calls": get_cell_value(target, 2),
            "mailing": get_cell_value(target, 3),
            "vk_requests": get_cell_value(target, 4),
            "vk_checks": get_cell_value(target, 5),
            "deadlines": get_cell_value(target, 6),
            "invoices": get_cell_value(target, 7),
            "payments_count": get_cell_value(target, 8),
            "payments_sum": get_cell_value(target, 9),
            "cvr": get_cell_value(target, 10),
            "non_quality": get_cell_value(target, 11)
        }
    except Exception as e:
        logger.error(f"❌ Ошибка get_manager_day_data: {e}", exc_info=True)
        return None

def format_manager_report(data: Dict[str, str]) -> str:
    report_date = get_now_moscow().strftime('%d.%m.%Y')
    return (
        f"📊 <b>ОТЧЕТ ПРИНЯТ</b>\n"
        f"──────────────────────\n"
        f"👤 <b>Менеджер:</b> {data['full_name']}\n"
        f"📅 <b>Дата:</b> {report_date}\n"
        f"──────────────────────\n"
        f"🔹 <b>Лиды:</b> {data['leads']}\n"
        f"🔹 <b>Звонки:</b> {data['calls']}\n"
        f"🔹 <b>Расслыка:</b> {data['mailing']}\n"
        f"🔹 <b>ВК записано:</b> {data['vk_requests']}\n"
        f"🔹 <b>ВК проведено:</b> {data['vk_checks']}\n"
        f"🔹 <b>Дедлайны:</b> {data['deadlines']}\n"
        f"🔹 <b>Счета:</b> {data['invoices']}\n"
        f"💰 <b>Оплаты:</b> {data['payments_count']} шт. на сумму {data['payments_sum']}₽\n"
        f"📈 <b>CVR:</b> {data['cvr']}%\n"
        f"🔹 <b>Некачественные:</b> {data['non_quality']}\n"
        f"──────────────────────"
    )

# ---------------------------------------------------------
# ОБРАБОТЧИКИ TELEGRAM
# ---------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Сдать отчёт", callback_data="submit_report")]])
    await update.message.reply_text("👋 Привет! Я бот для контроля отчётов.\nНажмите кнопку ниже, чтобы сдать отчёт.", reply_markup=kb)

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Сдать отчёт", callback_data="submit_report")]])
    await update.message.reply_text("📊 Нажмите кнопку, чтобы сдать отчёт:", reply_markup=kb)

async def process_callback_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    username = user.username

    if not username:
        await query.edit_message_text("❌ <b>Ошибка!</b>\nУ вас не заполнен Username в Telegram.", parse_mode='HTML')
        return

    spreadsheet = await get_spreadsheet()
    if not spreadsheet:
        await query.edit_message_text("❌ <b>Ошибка подключения к таблице!</b>", parse_mode='HTML')
        return

    try:
        ref_sheet = spreadsheet.worksheet("СПРАВОЧНИКИ")
        ref_data = ref_sheet.get_all_values()
        full_name = None

        for row in ref_data[1:]:
            if len(row) >= 2:
                ref_username = str(row[1]).strip().replace("@", "").lower()
                if ref_username == username.lower():
                    full_name = str(row[0]).strip()
                    break

        if not full_name:
            await query.edit_message_text(f"❌ <b>Вы не найдены в списке!</b>\nВаш username: @{username}", parse_mode='HTML')
            return

        active_managers = await get_active_managers_for_today()
        is_on_shift = any(match_names(m["full_name"], full_name) for m in active_managers)

        if not is_on_shift:
            current_date_str = get_now_moscow().strftime('%d.%m.%Y')
            await query.edit_message_text(f"❌ <b>Вы не на смене по графику!</b>\nСегодня ({current_date_str}) вас нет.", parse_mode='HTML')
            return

        manager_data = await get_manager_day_data(full_name)
        if not manager_data:
            await query.edit_message_text("❌ <b>Ошибка получения данных!</b>", parse_mode='HTML')
            return

        report_text = format_manager_report(manager_data)
        await context.bot.send_message(chat_id=CHAT_ID, text=report_text, parse_mode='HTML')

        current_time = get_now_moscow().strftime('%d.%m.%Y %H:%M')
        await query.edit_message_text(f"✅ <b>Отчёт успешно отправлен!</b>\n📅 {current_time}", parse_mode='HTML')

    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)
        await query.edit_message_text(f"❌ <b>Произошла ошибка!</b>\n{e}", parse_mode='HTML')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")

# ---------------------------------------------------------
# ФУНКЦИЯ ДЛЯ ЕЖЕДНЕВНЫХ НАПОМИНАНИЙ (Cron Job)
# ---------------------------------------------------------
async def send_reminder():
    """
    Отправляет напоминание в чат.
    Вызывается CRON JOB'ом на Render (каждые 5 минут проверяем время).
    """
    # Создаём Application только для отправки сообщения
    app = Application.builder().token(BOT_TOKEN).build()
    await app.initialize()
    await app.start()

    now = get_now_moscow()
    # Отправляем напоминание строго в нужное время
    # 14:50 и 19:30
    if (now.hour == 14 and now.minute == 50) or (now.hour == 19 and now.minute == 30):
        message = "⏰ <b>Напоминание:</b> Пришло время сдать отчёт! Нажмите кнопку в чате с ботом."
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='HTML')
            logger.info("✅ Напоминание отправлено")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки напоминания: {e}")

    await app.stop()
    await app.shutdown()

# ---------------------------------------------------------
# HANDLER ДЛЯ CRON JOB (Render будет вызывать этот эндпоинт)
# ---------------------------------------------------------
async def cron_handler(request):
    """
    Эндпоинт для HTTP-запроса от Render Cron Job.
    Render будет стучаться сюда каждые 5 минут.
    """
    logger.info("🔄 Получен запрос от Cron Job")
    try:
        await send_reminder()
        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"❌ Ошибка в cron_handler: {e}")
        return web.Response(text=f"Error: {e}", status=500)

# ---------------------------------------------------------
# WEBHOOK / HEALTH CHECK
# ---------------------------------------------------------
async def health_check(request):
    return web.Response(text="Bot is running!", status=200)

async def on_startup(app):
    logger.info("✅ Бот запускается...")

async def on_shutdown(app):
    logger.info("✅ Бот останавливается")

async def main():
    logger.info("🤖 Инициализация бота...")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("report", cmd_report))
    application.add_handler(CallbackQueryHandler(process_callback_submit, pattern="^submit_report$"))
    application.add_error_handler(error_handler)

    # Настройка aiohttp приложения
    web_app = web.Application()
    web_app.router.add_get('/', health_check)
    web_app.router.add_get('/health', health_check)
    web_app.router.add_get('/cron', cron_handler)  # 👈 Эндпоинт для Cron Job

    web_app.on_startup.append(on_startup)
    web_app.on_shutdown.append(on_shutdown)

    await application.initialize()

    # Настройка webhook или polling
    WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")
    if WEBHOOK_URL:
        WEBHOOK_PATH = "/webhook"
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}{WEBHOOK_PATH}")

        async def webhook_handler(request):
            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
            return web.Response(text="OK", status=200)

        web_app.router.add_post(WEBHOOK_PATH, webhook_handler)
        logger.info(f"🌐 Webhook установлен на {WEBHOOK_URL}{WEBHOOK_PATH}")
    else:
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("🔄 Запущен polling")

    # Запускаем HTTP сервер
    runner = web.AppRunner(web_app)
    await runner.setup()
    PORT = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

    logger.info(f"🚀 HTTP сервер на порту {PORT}")

    # Держим процесс живым
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
