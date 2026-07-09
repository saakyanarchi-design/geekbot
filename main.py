from datetime import datetime
from zoneinfo import ZoneInfo
import logging
import os
import sys
import json
import threading

import gspread
from google.oauth2 import service_account

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------------------------------------
# НАСТРОЙКА ЧАСОВОГО ПОЯСА
# ---------------------------------------------------------
TZ_MOSCOW = ZoneInfo("Europe/Moscow")

def get_now_moscow() -> datetime:
    return datetime.now(TZ_MOSCOW)

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
CHAT_ID = os.getenv("CHAT_ID")

# Проверка критических переменных сразу при старте
if not BOT_TOKEN:
    logger.error("❌ КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения BOT_TOKEN не найдена!")
    logger.error("Зайдите в Render Dashboard -> Settings -> Environment Variables и добавьте ключ BOT_TOKEN")
    sys.exit(1)

if not CHAT_ID:
    logger.warning("⚠️ CHAT_ID не найден, используется значение по умолчанию или будет запрошено позже.")
    # Если нужно жестко требовать CHAT_ID, раскомментируйте строку ниже:
    # sys.exit(1)
else:
    try:
        CHAT_ID = int(CHAT_ID)
    except ValueError:
        logger.error("❌ CHAT_ID должен быть числом (int). Текущее значение: {}".format(CHAT_ID))
        sys.exit(1)

REF_SHEET_NAME = "СПРАВОЧНИКИ"
SCHEDULE_SHEET_NAME = "График"

_spreadsheet_cache = None

# ---------------------------------------------------------
# GOOGLE SHEETS - ИНИЦИАЛИЗАЦИЯ
# ---------------------------------------------------------

def init_google_sheets() -> Optional[gspread.Spreadsheet]:
    try:
        spreadsheet_id = os.getenv("SPREADSHEET_ID")
        
        if not spreadsheet_id:
            logger.error("❌ Переменная окружения SPREADSHEET_ID не задана!")
            return None

        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        creds = None

        if creds_json and creds_json.strip().startswith("{"):
            creds_dict = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive.file"
                ]
            )
        else:
            raw_key = os.getenv("PRIVATE_KEY", "")
            clean_key = raw_key.replace("\\n", "\n")
            
            creds_dict = {
                "type": "service_account",
                "project_id": os.getenv("PROJECT_ID"),
                "private_key_id": os.getenv("PRIVATE_KEY_ID"),
                "private_key": clean_key,
                "client_email": os.getenv("CLIENT_EMAIL"),
                "client_id": os.getenv("CLIENT_ID"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_x509_cert_url": os.getenv("CLIENT_CERT_URL")
            }
            
            creds = service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive.file"
                ]
            )

        if not creds:
            logger.error("❌ Не удалось создать credentials")
            return None

        client = gspread.authorize(creds)
        logger.info(f"📂 Подключение к таблице по ID: {spreadsheet_id[:10]}...")
        
        spreadsheet = client.open_by_key(spreadsheet_id)
        logger.info(f"✅ Таблица успешно подключена: {spreadsheet.title}")
        return spreadsheet

    except gspread.exceptions.SpreadsheetNotFound:
        logger.error(f"❌ Таблица с ID {os.getenv('SPREADSHEET_ID')} не найдена.")
        return None
    except Exception as e:
        logger.error(f"❌ Критическая ошибка Google Sheets: {e}", exc_info=True)
        return None

def get_spreadsheet() -> Optional[gspread.Spreadsheet]:
    global _spreadsheet_cache
    
    if _spreadsheet_cache is None:
        logger.info("🔄 Инициализация клиента Google Sheets...")
        _spreadsheet_cache = init_google_sheets()
    
    if _spreadsheet_cache is None:
        logger.error("❌ Клиент не инициализирован или таблица недоступна")
        return None
    
    return _spreadsheet_cache

# ---------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (оставлены без изменений)
# ---------------------------------------------------------

def normalize_name(name_str: str) -> str:
    if not name_str:
        return ""
    clean = str(name_str).replace(".", "").strip().lower()
    return clean

def match_names(name_in_table: str, name_to_find: str) -> bool:
    t = normalize_name(name_in_table)
    f = normalize_name(name_to_find)
    
    if t == f:
        return True
    
    t_parts = t.split()
    f_parts = f.split()
    
    if not t_parts or not f_parts:
        return False

    if t_parts != f_parts:
        return False
    
    if len(t_parts) >= 2 and len(f_parts) >= 2:
        if t_parts == f_parts:
            return True
            
    return False

# ---------------------------------------------------------
# ЛОГИКА ДАННЫХ (оставлена без изменений)
# ---------------------------------------------------------

def get_active_managers_for_today() -> List[Dict[str, str]]:
    try:
        spreadsheet = get_spreadsheet()
        if not spreadsheet:
            return []

        try:
            ref_sheet = spreadsheet.worksheet(REF_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            logger.error(f"❌ Лист '{REF_SHEET_NAME}' не найден!")
            return []

        ref_data = ref_sheet.get_all_values()
        if len(ref_data) < 2:
            return []

        candidates = []
        for row in ref_data[1:]:
            if len(row) >= 2:
                name_val = str(row).strip()
                username_val = str(row).strip().replace("@", "").lower()
                
                if name_val and username_val:
                    candidates.append({
                        "full_name": name_val,
                        "username": username_val
                    })
                elif not username_val and name_val:
                    logger.debug(f"ℹ️ Сотрудник '{name_val}' исключен: пустой Username.")

        if not candidates:
            logger.warning("Нет активных кандидатов с заполненным Username")
            return []

        try:
            schedule_sheet = spreadsheet.worksheet(SCHEDULE_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            logger.error(f"❌ Лист '{SCHEDULE_SHEET_NAME}' не найден!")
            return []

        schedule_data = schedule_sheet.get_all_values()
        if not schedule_data:
            return []

        now = get_now_moscow()
        today_str = str(now.day)

        header = schedule_data if schedule_data else []
        col_idx = -1
        
        for i, val in enumerate(header):
            if str(val).strip() == today_str:
                col_idx = i
                break

        if col_idx == -1:
            logger.warning(f"Колонка даты {today_str} не найдена. Заголовки: {header}")
            return []

        schedule_map = {}
        for row in schedule_data[1:]:
            if len(row) > col_idx:
                name = str(row).strip()
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

def get_manager_day_data(full_name: str) -> Optional[Dict[str, str]]:
    try:
        spreadsheet = get_spreadsheet()
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
                logger.info(f"📄 Найден лист для отчета: {name}")
                break
            except gspread.exceptions.WorksheetNotFound:
                continue
        
        if not day_sheet:
            logger.error(f"❌ Лист для {day} не найден.")
            return None

        all_data = day_sheet.get_all_values()
        if not all_data:
            return None

        row_num = None
        for i, row in enumerate(all_data):
            if row and len(row) > 0:
                if match_names(str(row).strip(), full_name):
                    row_num = i
                    break

        if row_num is None:
            logger.warning(f"'{full_name}' не найден на листе {day_sheet.title}")
            return None

        target = all_data[row_num]
        
        def safe(col_index, default="0"):
            if col_index < len(target):
                v = str(target[col_index]).strip()
                return v if v else default
            return default

        return {
            "full_name": full_name,
            "leads": safe(1),
            "calls": safe(2),
            "mailing": safe(3),
            "vk_requests": safe(4),
            "vk_checks": safe(5),
            "deadlines": safe(6),
            "invoices": safe(7),
            "payments_count": safe(8),
            "payments_sum": safe(9),
            "cvr": safe(10),
            "non_quality": safe(11)
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
# ОБРАБОТЧИКИ TELEGRAM
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
            "Пожалуйста, установите его в настройках.",
            parse_mode='HTML'
        )
        return

    spreadsheet = get_spreadsheet()
    if not spreadsheet:
        await query.edit_message_text(
            "❌ <b>Ошибка подключения к таблице!</b>\n\n"
            "Сервис временно недоступен.",
            parse_mode='HTML'
        )
        return

    try:
        ref_sheet = spreadsheet.worksheet(REF_SHEET_NAME)
        ref_data = ref_sheet.get_all_values()

        full_name = None
        for row in ref_data[1:]:
            if len(row) >= 2:
                ref_username = str(row).strip().replace("@", "").lower()
                if ref_username == username.lower():
                    full_name = str(row).strip()
                    break

        if not full_name:
            await query.edit_message_text(
                f"❌ <b>Вы не найдены в списке менеджеров!</b>\n\n"
                f"Ваш username: @{username}\n"
                f"Обратитесь к администратору, чтобы вас добавили в лист '{REF_SHEET_NAME}'.",
                parse_mode='HTML'
            )
            return

        active_managers = get_active_managers_for_today()
        is_on_shift = any(match_names(m["full_name"], full_name) for m in active_managers)

        if not is_on_shift:
            current_date_str = get_now_moscow().strftime('%d.%m.%Y')
            await query.edit_message_text(
                f"❌ <b>Вы не на смене по графику!</b>\n\n"
                f"Сегодня ({current_date_str}) вас нет в листе '{SCHEDULE_SHEET_NAME}'.",
                parse_mode='HTML'
            )
            return

        manager_data = get_manager_day_data(full_name)

        if not manager_data:
            await query.edit_message_text(
                f"❌ <b>Ошибка получения данных!</b>\n\n"
                f"Не удалось найти ваши данные на листе текущего дня.",
                parse_mode='HTML'
            )
            return

        report_text = format_manager_report(manager_data)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=report_text,
            parse_mode='HTML'
        )

        current_time = get_now_moscow().strftime('%d.%m.%Y %H:%M')
        await query.edit_message_text(
            f"✅ <b>Отчёт успешно отправлен!</b>\n\n"
            f"📅 {current_time}",
            parse_mode='HTML'
        )

    except gspread.exceptions.WorksheetNotFound as e:
        logger.error(f"Лист не найден: {e}")
        await query.edit_message_text(
            "❌ <b>Ошибка структуры таблицы!</b>\n\n"
            "Один из необходимых листов отсутствует.",
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке отчёта: {e}", exc_info=True)
        await query.edit_message_text(
            f"❌ <b>Произошла внутренняя ошибка!</b>\n\n"
            f"Ошибка: {str(e)}\n"
            f"Сообщите администратору.",
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
        logger.info(f"✅ Напоминание отправлено. Менеджеров: {len(managers)}")
    except Exception as e:
        logger.error(f"❌ Ошибка в send_reminder: {e}")

# ---------------------------------------------------------
# ЗАПУСК (ИСПРАВЛЕННАЯ ВЕРСИЯ)
# ---------------------------------------------------------

def main():
    """Точка входа. Запускает только то, что нужно для Render."""
    
    logger.info("🚀 Инициализация бота...")

    # 1. Инициализация Google Sheets (синхронно перед запуском бота)
    global _spreadsheet_cache
    _spreadsheet_cache = init_google_sheets()
    
    if _spreadsheet_cache is None:
        logger.error("❌ Критическая ошибка: не удалось подключиться к Google Sheets. Остановка.")
        sys.exit(1)

    # 2. Создание приложения бота
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("report", cmd_report))
    application.add_handler(CallbackQueryHandler(process_callback_submit, pattern="submit_report"))
    application.add_error_handler(error_handler)

    # 3. Настройка планировщика
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_reminder, 'cron', hour=14, minute=50, args=[application.bot])
    scheduler.add_job(send_reminder, 'cron', hour=19, minute=30, args=[application.bot])
    scheduler.start()
    logger.info("✅ Планировщик запущен (14:50, 19:30 МСК)")

    # 4. Удаление Webhook (чтобы избежать конфликтов при деплое)
    try:
        application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook удалён, все ожидающие обновления сброшены")
    except Exception as e:
        logger.warning(f"Не удалось удалить webhook: {e}")

    # 5. ЗАПУСК БОТА
    # ВАЖНО: Мы НЕ используем asyncio.run() здесь.
    # application.run_polling() сам управляет своим event loop.
    # Health Check сервер УДАЛЕН, так как он конфликтовал с Render.
    
    logger.info("🚀 Запуск бота (run_polling)...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=30
    )

if __name__ == "__main__":
    main()
