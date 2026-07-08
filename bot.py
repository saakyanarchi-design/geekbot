import asyncio
import datetime
import logging
import os
from typing import List, Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
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
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Берём из переменных окружения Render
CHAT_ID = int(os.getenv("CHAT_ID", "-10013813207765"))  # Исправлено: убрал лишний 0
SPREADSHEET_NAME = "Отчёты/ Срезы ОТБ Июль"
REF_SHEET_NAME = "СПРАВОЧНИКИ"
SCHEDULE_SHEET_NAME = "График"

# ---------------------------------------------------------
# ПОДГОТОВКА GOOGLE SHEETS
# ---------------------------------------------------------
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
client = None

def init_google_sheets():
    """Инициализация клиента Google Sheets"""
    global client
    try:
        # В Render credentials хранятся в переменной окружения
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if not creds_json:
            logger.error("❌ GOOGLE_CREDENTIALS не найдены в переменных окружения!")
            return None

        import json
        creds_dict = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        logger.info("✅ Google Sheets авторизован")
        return client
    except Exception as e:
        logger.error(f"❌ Ошибка авторизации Google Sheets: {e}")
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

def get_active_managers_for_today() -> List[Dict[str, str]]:
    """Получение списка активных менеджеров на сегодня"""
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
                    "username": row[1].strip()
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

        today_str = datetime.datetime.now().strftime("%d")
        header_row = schedule_data[0]
        target_col_index = -1

        for idx, val in enumerate(header_row):
            clean_val = str(val).strip()
            if clean_val == today_str or clean_val == str(int(today_str)):
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
                is_working = str(status_cell).strip().lower() in ["true", "1", "да", "✓"]
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
# ОБРАБОТЧИКИ КОМАНД
# ---------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /start"""
    await update.message.reply_text(
        "👋 Привет! Я бот для контроля отчётов ОТБ.\n"
        "Нажмите кнопку ниже, чтобы сдать отчёт."
    )

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /report"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сдать отчёт", callback_data="submit_report")]
    ])
    await update.message.reply_text("📊 Выберите действие:", reply_markup=kb)

async def process_callback_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Сдать отчёт'"""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    username = user.username

    if not username:
        await query.answer("❌ У вас нет username в Telegram!", show_alert=True)
        return

    spreadsheet = get_spreadsheet()
    if not spreadsheet:
        await query.answer("❌ Ошибка подключения к таблице!", show_alert=True)
        return

    try:
        ref_sheet = spreadsheet.worksheet(REF_SHEET_NAME)
        ref_data = ref_sheet.get_all_values()

        full_name = None
        for row in ref_data[1:]:
            if len(row) >= 2 and row[1].strip().lower() == username.lower():
                full_name = row[0].strip()
                break

        if not full_name:
            await query.answer(
                f"❌ Вы (@{username}) не найдены в списке менеджеров!", 
                show_alert=True
            )
            return

        # Проверка графика
        active_managers = get_active_managers_for_today()
        is_on_shift = any(m["full_name"] == full_name for m in active_managers)

        if not is_on_shift:
            await query.answer(
                "❌ Вы сейчас не на смене по графику!", 
                show_alert=True
            )
            return

        # Отправка отчёта в группу
        current_time = datetime.datetime.now().strftime('%d.%m.%Y %H:%M')
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"📋 <b>Отчёт сдан!</b>\n\n"
                 f"👤 Менеджер: {full_name} (@{username})\n"
                 f"📅 Дата: {current_time}",
            parse_mode='HTML'
        )

        # Подтверждение в личку
        await query.edit_message_text(
            f"✅ <b>Отчёт успешно отправлен!</b>\n\n"
            f"📅 {current_time}",
            parse_mode='HTML'
        )

    except Exception as e:
        logger.error(f"Ошибка при отправке отчёта: {e}")
        await query.answer("❌ Произошла ошибка!", show_alert=True)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ Произошла ошибка. Попробуйте позже."
        )

# ---------------------------------------------------------
# ПЛАНИРОВЩИК
# ---------------------------------------------------------

async def send_reminder(bot: Application.bot):
    """Отправка напоминания в группу"""
    try:
        managers = get_active_managers_for_today()
        if not managers:
            logger.info("Сегодня нет активных менеджеров")
            return

        names = "\n".join([f"• {m['full_name']} (@{m['username']})" for m in managers])
        msg = f"⏰ <b>Напоминание!</b>\n\nСегодня на смене:\n{names}\n\nПора сдать отчёт! 📋"

        await bot.send_message(
            chat_id=CHAT_ID,
            text=msg,
            parse_mode='HTML'
        )
        logger.info(f"Напоминание отправлено. Менеджеров: {len(managers)}")

    except Exception as e:
        logger.error(f"Ошибка в напоминалке: {e}")

# ---------------------------------------------------------
# ЗАПУСК БОТА
# ---------------------------------------------------------

async def main():
    """Главная функция запуска"""
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
    application.add_error_handler(error_handler)

    # Настраиваем планировщик
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_reminder,
        'cron',
        hour=9,
        minute=0,
        args=[application.bot]
    )
    scheduler.start()
    logger.info("✅ Планировщик запущен (напоминание в 9:00)")

    # Запускаем бота с очисткой старых обновлений
    logger.info("🤖 Бот запущен и готов к работе!")

    # Важно: удаляем вебхук и сбрасываем старые обновления
    await application.bot.delete_webhook(drop_pending_updates=True)

    # Запускаем polling
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    # Держим бота активным
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
        scheduler.shutdown()
        await application.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем.")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
