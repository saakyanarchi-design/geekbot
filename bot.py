import logging
import os
from datetime import datetime
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from oauth2client.service_account import ServiceAccountCredentials

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Настройки ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GOOGLE_SHEETS_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
SHEET_NAME = "Отчёты/ Срезы ОТБ Июль"

# --- Подключение к Google Sheets ---
def get_google_sheet():
    try:
        # Если credentials переданы как строка JSON из переменной окружения
        import json
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME)
        logger.info("✅ Успешное подключение к Google Sheets")
        return sheet
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Google Sheets: {e}")
        return None

# --- Проверка, может ли пользователь сдать отчёт ---
def check_can_submit_report(username):
    try:
        # 1. Получаем имя сотрудника из листа "СПРАВОЧНИКИ"
        sheet = get_google_sheet()
        if not sheet:
            return False, "❌ Ошибка подключения к базе данных."

        sheet_refs = sheet.worksheet("СПРАВОЧНИКИ")
        all_refs = sheet_refs.get_all_values()

        # Пропускаем заголовок (первая строка), если он есть
        start_row = 1 if all_refs and all_refs[0][0] in ["Имя", "Сотрудник", ""] else 0

        user_name = None
        for row in all_refs[start_row:]:
            if len(row) > 1 and row[1].strip() == username:  # колонка B (никнейм)
                user_name = row[0].strip()  # колонка A (имя)
                break

        if not user_name:
            return False, "❌ Вы не найдены в базе или ваш никнейм не привязан."

        logger.info(f"✅ Найден сотрудник: {user_name}")

        # 2. Проверяем график на сегодня
        sheet_graph = sheet.worksheet("График")
        data = sheet_graph.get_all_values()

        if not data or len(data) < 2:
            return False, "❌ Ошибка: лист 'График' пуст или содержит недостаточно данных."

        # Заголовки (первая строка) - дни месяца
        headers = data[0]
        today_day = str(datetime.now().day)  # сегодня 8-е число

        # Ищем колонку с сегодняшним числом
        col_index = -1
        for i, h in enumerate(headers):
            if h and h.strip() == today_day:
                col_index = i
                break

        if col_index == -1:
            return False, f"❌ Ошибка: в таблице не найдена колонка с датой '{today_day}'."

        # Ищем строку с именем сотрудника
        row_index = -1
        for i, row in enumerate(data):
            if row and row[0].strip() == user_name:
                row_index = i
                break

        if row_index == -1:
            return False, f"❌ Вы ({user_name}) не найдены в списке на листе 'График'."

        # 3. Проверяем значение ячейки (чекбокс)
        cell_value = data[row_index][col_index].strip() if col_index < len(data[row_index]) else "FALSE"

        # Чекбокс может быть "TRUE", "True", True или просто "1"
        if cell_value in ["TRUE", "True", "true", "1", True]:
            return True, f"✅ Вы на смене, {user_name}! Нажмите кнопку ниже, чтобы начать отчёт."
        else:
            return False, f"⛔ Сегодня у вас нет смены по графику (галочка не отмечена)."

    except Exception as e:
        logger.error(f"❌ Ошибка проверки: {e}")
        return False, f"❌ Произошла ошибка при проверке графика: {str(e)}"

# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username

    logger.info(f"Пользователь @{username} запустил /start")

    if not username:
        await update.message.reply_text(
            "❌ У вас не установлен username в Telegram.\n"
            "Пожалуйста, установите его в настройках Telegram и попробуйте снова."
        )
        return

    # Клавиатура с главными кнопками
    keyboard = [
        [KeyboardButton("✅ Сдать отчёт")],
        [KeyboardButton("❓ Помощь")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n"
        f"Я бот для сдачи отчётов.\n\n"
        f"Нажмите «✅ Сдать отчёт», чтобы начать.",
        reply_markup=reply_markup
    )

# --- Команда /report ---
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username

    if not username:
        await update.message.reply_text("❌ У вас не установлен username в Telegram.")
        return

    can_report, message = check_can_submit_report(username)

    if can_report:
        # Создаем инлайн-клавиатуру для подтверждения
        keyboard = [
            [InlineKeyboardButton("✅ Да, я на смене", callback_data='confirm_report')],
            [InlineKeyboardButton("❌ Отмена", callback_data='cancel_report')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            message,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(message)

# --- Обработка нажатий на инлайн-кнопки ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'confirm_report':
        await query.edit_message_text(text="📝 Отлично! Начинаем сдачу отчёта...\n\n(Здесь будет логика сбора данных)")
        # Здесь можно добавить дальнейшую логику сбора отчёта
    elif query.data == 'cancel_report':
        await query.edit_message_text(text="❌ Отчёт отменён.")

# --- Обработка текстовых сообщений ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    username = update.effective_user.username

    if text == "✅ Сдать отчёт":
        if not username:
            await update.message.reply_text("❌ У вас не установлен username в Telegram.")
            return

        can_report, message = check_can_submit_report(username)
        await update.message.reply_text(message)

    elif text == "❓ Помощь":
        await update.message.reply_text(
            "ℹ️ Как пользоваться ботом:\n\n"
            "1. Убедитесь, что у вас установлен username в Telegram\n"
            "2. Ваш username должен совпадать с никнеймом в таблице (лист 'СПРАВОЧНИКИ')\n"
            "3. Нажмите «✅ Сдать отчёт»\n"
            "4. Бот проверит вашу смену по графику\n"
            "5. Если всё ОК — можно сдавать отчёт\n\n"
            "Если возникли проблемы — обратитесь к администратору."
        )
    else:
        await update.message.reply_text(
            "Используйте кнопки внизу экрана:\n"
            "✅ Сдать отчёт — проверить график и начать отчёт\n"
            "❓ Помощь — инструкция"
        )

# --- Обработка ошибок ---
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ Произошла ошибка. Попробуйте позже."
        )

# --- Главная функция ---
def main():
    if not BOT_TOKEN:
        logger.error("❌ Не указан BOT_TOKEN в переменных окружения!")
        return

    if not GOOGLE_SHEETS_CREDENTIALS:
        logger.error("❌ Не указаны GOOGLE_CREDENTIALS в переменных окружения!")
        return

    # Создаем приложение
    app = Application.builder().token(BOT_TOKEN).build()

    # Регистрируем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # Запускаем бота
    logger.info("🚀 Бот запущен и готов к работе!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
