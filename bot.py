import logging
import pandas as pd
import requests
import io
import os
import asyncio
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import sys

# --- Импорты для Selenium ---
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, constants
from telegram.error import Forbidden
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)

# --- НАСТРОЙКА ПРОКСИ ДЛЯ PYTHONANYWHERE ---
# Это необходимо для бесплатных аккаунтов, чтобы requests работал
if 'pythonanywhere' in sys.executable:
    proxy_url = 'http://proxy.server:3128'
    os.environ['http_proxy'] = proxy_url
    os.environ['https_proxy'] = proxy_url

# --- НАСТРОЙКИ ---
# ВАЖНО: Убедитесь, что вы вставили свой актуальный токен
TELEGRAM_TOKEN = "8408376268:AAHMKXSFVCZ6meJF603myAG_8SWJCHa7GK0" 
SCHEDULE_PAGE_URL = "https://sh40-cherepovec-r19.gosweb.gosuslugi.ru/roditelyam-i-uchenikam/izmeneniya-v-raspisanii/"
LINK_KEYWORDS = "1 смена ШРК"
TARGET_CLASS = "9г"
# --- НАСТРОЙКИ ДЛЯ УВЕДОМЛЕНИЙ ---
SUBSCRIBERS_FILE = "subscribers.txt"
CHECK_INTERVAL_SECONDS = 1800
STATE_FILE = "last_schedule_url.txt"
# --- ТЕКСТ КНОПОК ---
SCHEDULE_BUTTON_TEXT = f"Узнать расписание {TARGET_CLASS.upper()} 📋"
NOTIFY_ON_TEXT = "Включить Уведомления 🔔"
NOTIFY_OFF_TEXT = "Выключить Уведомления 🔕"
# --- КОНЕЦ НАСТРОЕК ---

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ПРАВИЛЬНАЯ ФУНКЦИЯ ПОИСКА ССЫЛКИ С SELENIUM ---
def find_latest_schedule_info() -> tuple[str, str] | None:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # Указываем пути, которые использует PythonAnywhere
    service = Service(executable_path='/usr/bin/chromedriver')
    
    with webdriver.Chrome(service=service, options=options) as driver:
        try:
            driver.get(SCHEDULE_PAGE_URL)
            wait = WebDriverWait(driver, 20) # Увеличим время ожидания
            wait.until(EC.presence_of_element_located((By.PARTIAL_LINK_TEXT, "смена")))
            soup = BeautifulSoup(driver.page_source, 'lxml')
            
            for link in soup.find_all('a', href=True):
                if LINK_KEYWORDS.lower() in link.text.lower():
                    full_url = urljoin(SCHEDULE_PAGE_URL, link['href'])
                    logger.info(f"Найдена актуальная ссылка: {full_url}")
                    return full_url, link.text.strip()
            
            logger.warning(f"Ссылка с ключевыми словами '{LINK_KEYWORDS}' не найдена.")
            return None
        except Exception as e:
            logger.error(f"Ошибка при работе Selenium на PythonAnywhere: {e}")
            return None

# --- ВСЕ ОСТАЛЬНЫЕ ФУНКЦИИ ---
def load_subscribers() -> set[int]:
    if not os.path.exists(SUBSCRIBERS_FILE): return set()
    with open(SUBSCRIBERS_FILE, "r") as f:
        return {int(line.strip()) for line in f if line.strip()}

def save_subscribers(subscribers: set[int]) -> None:
    with open(SUBSCRIBERS_FILE, "w") as f:
        for user_id in subscribers: f.write(str(user_id) + "\n")

def parse_schedule_for_class(df: pd.DataFrame, class_name: str) -> str:
    class_coords, time_coords = None, None
    class_name_clean = class_name.lower().strip()
    for r in range(min(15, len(df))):
        for c in range(len(df.columns)):
            cell_value = df.iloc[r, c]
            if pd.notna(cell_value):
                cell_str_clean = str(cell_value).lower().strip()
                if cell_str_clean == class_name_clean: class_coords = (r, c)
                if "время" in cell_str_clean: time_coords = (r, c)
    if class_coords is None: return f"Не удалось найти столбец для класса '{class_name.upper()}'."
    if time_coords is None: return "Не удалось найти столбец 'Время'."
    target_col_idx, time_col_idx, start_row_idx = class_coords[1], time_coords[1], class_coords[0] + 1
    schedule_lines = []
    i = start_row_idx
    while i < len(df):
        row = df.iloc[i]
        lesson_time, subject = row.get(time_col_idx), row.get(target_col_idx)
        if pd.notna(subject) and str(subject).strip():
            line = f"• ({str(lesson_time).strip()}) **{str(subject).strip()}**"
            if i + 1 < len(df):
                next_row = df.iloc[i+1]
                cabinet_info = next_row.get(target_col_idx)
                if pd.notna(cabinet_info) and str(cabinet_info).strip() and pd.isna(next_row.get(time_col_idx)):
                    line += f" — _{str(cabinet_info).strip()}_"
                    i += 1
            schedule_lines.append(line)
        i += 1
    if not schedule_lines: return f"Для класса '{class_name.upper()}' уроков не найдено."
    return "\n".join(schedule_lines)

def get_main_keyboard(user_id: int, subscribers: set[int]) -> ReplyKeyboardMarkup:
    notification_button_text = NOTIFY_OFF_TEXT if user_id in subscribers else NOTIFY_ON_TEXT
    keyboard = [[KeyboardButton(SCHEDULE_BUTTON_TEXT)],[KeyboardButton(notification_button_text)]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    subscribers = context.bot_data.get('subscribers', set())
    keyboard = get_main_keyboard(user_id, subscribers)
    await update.message.reply_text("Привет! Нажми на кнопку, чтобы узнать расписание или настроить уведомления.", reply_markup=keyboard)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if text == SCHEDULE_BUTTON_TEXT: await send_schedule(update, context)
    elif text in [NOTIFY_ON_TEXT, NOTIFY_OFF_TEXT]: await toggle_notifications_reply(update, context)

async def send_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    placeholder_message = await update.message.reply_text("Ищу расписание...")
    final_text = ""
    try:
        schedule_info = find_latest_schedule_info()
        if not schedule_info:
            final_text = f"Не удалось найти на сайте файл с расписанием, содержащий слова '{LINK_KEYWORDS}'."
        else:
            schedule_url, link_text = schedule_info
            response = requests.get(schedule_url)
            response.raise_for_status()
            file_content = io.BytesIO(response.content)
            df = None
            try: df = pd.read_excel(file_content, header=None, engine='openpyxl')
            except Exception:
                file_content.seek(0)
                df = pd.read_excel(file_content, header=None, engine='xlrd')
            parsed_schedule = parse_schedule_for_class(df, TARGET_CLASS)
            final_text = (f"📄 **Изменения для {TARGET_CLASS.upper()}**\n_{link_text}_\n\n{parsed_schedule}")
    except Exception as e:
        logger.error(f"Ошибка при получении расписания: {e}")
        final_text = "Произошла непредвиденная ошибка при обработке файла."
    await context.bot.edit_message_text(text=final_text, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=constants.ParseMode.MARKDOWN, disable_web_page_preview=True)

async def toggle_notifications_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    subscribers = context.bot_data.setdefault('subscribers', set())
    reply_text = ""
    if user_id in subscribers:
        subscribers.remove(user_id)
        reply_text = "Уведомления выключены."
    else:
        subscribers.add(user_id)
        reply_text = "Уведомления включены!"
    save_subscribers(subscribers)
    keyboard = get_main_keyboard(user_id, subscribers)
    await update.message.reply_text(reply_text, reply_markup=keyboard)

async def check_for_new_schedule(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Запущена периодическая проверка расписания...")
    schedule_info = find_latest_schedule_info()
    if not schedule_info: return
    current_url, link_text = schedule_info
    last_seen_url = ""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f: last_seen_url = f.read().strip()
    if current_url != last_seen_url:
        logger.info(f"!!! НАЙДЕНО НОВОЕ РАСПИСАНИЕ: {current_url}")
        with open(STATE_FILE, "w") as f: f.write(current_url)
        notification_text = f"🔔 **Появилось новое расписание!** 🔔\n\n_{link_text}_"
        subscribers = context.bot_data.get('subscribers', set()).copy()
        for user_id in subscribers:
            try:
                keyboard = get_main_keyboard(user_id, subscribers)
                await context.bot.send_message(chat_id=user_id, text=notification_text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=keyboard)
                await asyncio.sleep(0.1)
            except Forbidden:
                context.bot_data['subscribers'].remove(user_id)
                save_subscribers(context.bot_data['subscribers'])
            except Exception as e: logger.error(f"Ошибка отправки уведомления {user_id}: {e}")

def main() -> None:
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.bot_data['subscribers'] = load_subscribers()
    logger.info(f"Загружено {len(application.bot_data['subscribers'])} подписчиков.")
    job_queue = application.job_queue
    job_queue.run_repeating(check_for_new_schedule, interval=CHECK_INTERVAL_SECONDS, first=15)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Бот запущен и готов к работе...")
    application.run_polling()

if __name__ == "__main__":
    main()