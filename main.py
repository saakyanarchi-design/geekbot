import os
import sys
import logging
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2 import service_account

# Настройка логов
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TZ_MOSCOW = ZoneInfo("Europe/Moscow")

def get_now_moscow() -> datetime:
    return datetime.now(TZ_MOSCOW)

def init_google_sheets():
    try:
        spreadsheet_id = os.getenv("SPREADSHEET_ID")
        logger.info(f"📂 Пытаюсь подключиться к таблице с ID: {spreadsheet_id}")
        
        if not spreadsheet_id:
            logger.error("❌ Переменная SPREADSHEET_ID не найдена!")
            return None

        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        creds = None

        # Попытка прочитать JSON целиком
        if creds_json and creds_json.strip().startswith("{"):
            creds_dict = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        else:
            # Попытка собрать из частей (если ты используешь старые переменные)
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
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )

        if not creds:
            logger.error("❌ Не удалось создать credentials (пустой объект)")
            return None

        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(spreadsheet_id)
        logger.info(f"✅ УСПЕХ! Таблица подключена: {spreadsheet.title}")
        return spreadsheet

    except gspread.exceptions.SpreadsheetNotFound:
        logger.error(f"❌ ОШИБКА: Таблица с ID '{os.getenv('SPREADSHEET_ID')}' не найдена в Google Sheets!")
        return None
    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА ПОДКЛЮЧЕНИЯ: {e}", exc_info=True)
        return None

def main():
    logger.info("🚀 Запуск бота...")
    
    # 1. Проверка токена
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("❌ КРИТИЧЕСКАЯ ОШИБКА: Переменная BOT_TOKEN пуста!")
        sys.exit(1)
    
    # Скрываем токен в логах, но показываем, что он есть
    logger.info(f"🤖 Токен получен (первые 5 символов: {bot_token[:5]}...)")

    # 2. Проверка подключения к таблице (САМОЕ ВАЖНОЕ)
    logger.info("🔍 Проверка подключения к Google Sheets...")
    sheet = init_google_sheets()
    
    if sheet is None:
        logger.error("❌ Не удалось подключиться к таблице. Бот останавливается.")
        sys.exit(1)
    
    logger.info("✅ Все проверки пройдены. Запуск основного цикла...")
    
    # Здесь должен быть твой реальный код запуска бота (application.run_polling...)
    # Но пока мы оставили только проверки, чтобы увидеть ошибку.
    # Если дойдем сюда — значит, проблема была не в подключении.
    
    while True:
        logger.info("Бот работает... (заглушка)")
        import time
        time.sleep(60)

if __name__ == "__main__":
    main()
