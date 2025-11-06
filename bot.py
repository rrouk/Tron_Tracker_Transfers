import os
import time
import asyncio
import httpx
import base58
import requests
import aiosqlite
import hashlib
import threading
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from typing import Union
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    Application
)
from decimal import Decimal, getcontext, ROUND_DOWN
from datetime import datetime, timedelta
import logging
# Настройка логирования в начале файла (должна быть)
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(processName)s - %(name)s - %(levelname)s - %(message)s')


# 👥 СПИСОК ID АДМИНИСТРАТОРОВ
# Чтение строки ID из .env (например, "6887512338, 7463213193") и преобразование в список int.
# Если переменная не задана, используется пустой список.
ADMIN_IDS = []
admin_ids_str = os.getenv("ADMIN_CHAT_IDS")
if admin_ids_str:
    try:
        ADMIN_IDS = [int(i.strip()) for i in admin_ids_str.split(',')]
    except ValueError:
        logging.error("FATAL: ADMIN_CHAT_IDS в .env содержит нечисловые значения.")
        ADMIN_IDS = []


# настройка тредов
admin_group = int(os.getenv("admin_group"))
thread_energy = int(os.getenv("thread_energy"))
thread_trx = int(os.getenv("thread_trx"))
thread_usdt = int(os.getenv("thread_usdt"))
thread_bw = int(os.getenv("thread_bw"))

# 🔧 Настройки
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


# 🔑 СПИСОК КЛЮЧЕЙ ДЛЯ РОТАЦИИ
# Берет DEFAULT_TRONGRID_API_KEY из переменных окружения и добавляет запасные
DEFAULT_TRONGRID_API_KEY = os.getenv("DEFAULT_TRONGRID_API_KEY")
DEFAULT_API_KEYS = [
    DEFAULT_TRONGRID_API_KEY, # Ваш основной ключ
    os.getenv("BACKUP_TRONGRID_API_KEY_1"), # Ключ 1
    os.getenv("BACKUP_TRONGRID_API_KEY_2"), # Ключ 2
    os.getenv("BACKUP_TRONGRID_API_KEY_3"), # Ключ 3
    os.getenv("BACKUP_TRONGRID_API_KEY_4"), # Ключ 4
    # ... добавьте столько, сколько нужно
]
# Очищаем список от None или пустых строк, если переменные не заданы
DEFAULT_API_KEYS = [k for k in DEFAULT_API_KEYS if k]

# ❗ НОВЫЕ ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ КОНТРОЛЯ СКОРОСТИ (QPS)
# Создаем пул объектов. Каждый объект хранит ключ, семафор (для контроля доступа)
# и время последнего запроса (для расчета паузы).
KEY_POOL = [{'key': k, 'semaphore': asyncio.Semaphore(1), 'last_request_time': 0.0} 
            for k in DEFAULT_API_KEYS]
key_pool_index = 0

# Глобальный кэш семафоров и времени для ВСЕХ ключей (включая пользовательские)
KEY_SEMAPHORES = {}

DATABASE_FILE = "/app/data/user_data.db"
db_conn: aiosqlite.Connection = None

# Лимит QPS на один ключ (в секундах).
# По умолчанию: 1.0 (1 запрос в секунду)
# Если вы на платном тарифе 10 QPS, установите 0.1
QPS_LIMIT_SECONDS = float(os.getenv("TRONGRID_QPS_LIMIT_SECONDS", 1.0))

Pause_txid_get_tronscan = int(os.getenv("Pause_txid_get_tronscan", 1))

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS")) # пауза между циклами проверок
limit_txhd = int(os.getenv("limit_txhd")) # сколько грузим транзакций за проверку

USDT_CONTRACT_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t".lower()
hash_hash = "#"
timedelta_hours = int(os.getenv("timedelta_hours")) # +3 часа





# Логгинг
CRASH_Energyfile = "/app/log/Energy_Error.log"
# USER_DATA_FILE больше не нужен, т.к. используется БД
getcontext().rounding = ROUND_DOWN

user_data_lock = asyncio.Lock()


#**********************************************
# Удаление кошелька пользователя, заблокировавшего бота
#**********************************************
async def delete_user_wallets_and_data(chat_id: int):
    """Удаляет все кошельки и данные пользователя из базы данных после блокировки бота."""
    
    logging.info(f"🗑️ Запуск удаления данных для пользователя {chat_id} (бот заблокирован).")
    
    try:
        async with aiosqlite.connect(DATABASE_FILE) as db:
            # 1. Удаляем все записи кошельков, связанные с этим chat_id
            await db.execute("DELETE FROM addresses WHERE user_chat_id = ?", (chat_id,))
            await db.commit()
            logging.info(f"✅ Данные и кошельки пользователя {chat_id} успешно удалены.")
            
    except Exception as e:
        # Используйте вашу функцию логгирования ошибок
        log_error_crash(f"Критическая ошибка при удалении данных пользователя {chat_id}: {e}")


#**********************************************
# Синхронизация базы данных с .WAL
#**********************************************
async def flush_wal_to_db():
    """
    Сливает все изменения из WAL-файла в основную базу .db.
    Можно вызывать в любом месте асинхронного кода.
    """
    try:
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            await db.commit()
        logging.info("WAL успешно слит в базу.")
    except Exception as e:
        logging.info(f"Ошибка при сливе WAL: {e}")



#**********************************************
# 🔗 валидация ключа при добавлении
#**********************************************
async def is_valid_trongrid_key(api_key: str) -> bool:
    try:
        # Простой тестовый запрос (без нагрузки)
        url = "https://api.trongrid.io/wallet/getnowblock"
        headers = {"TRON-PRO-API-KEY": api_key}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
        return response.status_code == 200
    except:
        return False






#**********************************************
# 🔗 Инициализация БД (SQLite)
#**********************************************
async def init_db():
    global db_conn

    db_conn = await aiosqlite.connect(
        database=DATABASE_FILE, 
        timeout=15.0, 
        isolation_level=None
    )

    await db_conn.execute("PRAGMA journal_mode = WAL")
    await db_conn.execute("PRAGMA busy_timeout = 15000") # 15000 миллисекунд (15 секунд)

    async with db_conn.cursor() as cursor:
        # 1. Создаём таблицу users, если её нет
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                trongrid_api_key TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Проверяем существующие колонки
        await cursor.execute("PRAGMA table_info(users)")
        columns = await cursor.fetchall()
        column_names = {col[1] for col in columns}

        # 3. Добавляем колонки только если их нет
        if "monitor_energy" not in column_names:
            await cursor.execute("ALTER TABLE users ADD COLUMN monitor_energy BOOLEAN DEFAULT 1")
        if "monitor_trx" not in column_names:
            await cursor.execute("ALTER TABLE users ADD COLUMN monitor_trx BOOLEAN DEFAULT 1")
        if "monitor_usdt" not in column_names:
            await cursor.execute("ALTER TABLE users ADD COLUMN monitor_usdt BOOLEAN DEFAULT 1")
        if "invalid_key" not in column_names:
            await cursor.execute("ALTER TABLE users ADD COLUMN invalid_key BOOLEAN DEFAULT 0")
        if "monitor_bw" not in column_names:
            await cursor.execute("ALTER TABLE users ADD COLUMN monitor_bw BOOLEAN DEFAULT 1")

        # Убедимся, что у всех NULL → 0
        await cursor.execute("UPDATE users SET invalid_key = COALESCE(invalid_key, 0)")

        # 4. Убеждаемся, что у всех строк значения не NULL (на случай, если DEFAULT не сработал)
        await cursor.execute("""
            UPDATE users 
            SET 
                monitor_energy = COALESCE(monitor_energy, 1),
                monitor_trx = COALESCE(monitor_trx, 1),
                monitor_usdt = COALESCE(monitor_usdt, 1),
                monitor_bw = COALESCE(monitor_bw, 1)
        """)

        # 5. Создаём таблицу addresses
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_chat_id INTEGER,
                address TEXT NOT NULL,
                last_checked INTEGER DEFAULT 0,
                FOREIGN KEY (user_chat_id) REFERENCES users(chat_id) ON DELETE CASCADE,
                UNIQUE (user_chat_id, address)
            )
        """)

    await db_conn.commit()

#**********************************************
# 📁 Данные отслеживания переключателей в базе
#**********************************************
async def get_monitoring_settings(chat_id: int):
    async with db_conn.cursor() as cursor:
        await cursor.execute(
            "SELECT monitor_energy, monitor_trx, monitor_usdt, monitor_bw FROM users WHERE chat_id = ?",
            (chat_id,)
        )
        row = await cursor.fetchone()
        if row:
            return {
                "energy": bool(row[0]),
                "trx": bool(row[1]),
                "usdt": bool(row[2]),
                "bw": bool(row[3])
            }
        return {"energy": True, "trx": True, "usdt": True, "bw": True}  # fallback

async def toggle_monitoring(chat_id: int, setting: str, enabled: bool):
    column_map = {
        "energy": "monitor_energy",
        "trx": "monitor_trx",
        "usdt": "monitor_usdt",
        "bw": "monitor_bw"
    }
    col = column_map.get(setting)
    if not col:
        raise ValueError("Invalid setting")
    async with db_conn.cursor() as cursor:
        await cursor.execute(
            f"UPDATE users SET {col} = ? WHERE chat_id = ?",
            (int(enabled), chat_id)
        )
    await db_conn.commit()



#**********************************************
# 📁 Функции для работы с флагом апи ключа
#**********************************************
async def mark_key_as_invalid(chat_id: int):
    async with db_conn.cursor() as cursor:
        await cursor.execute("UPDATE users SET invalid_key = 1 WHERE chat_id = ?", (chat_id,))
    await db_conn.commit()

async def clear_invalid_key_flag(chat_id: int):
    async with db_conn.cursor() as cursor:
        await cursor.execute("UPDATE users SET invalid_key = 0 WHERE chat_id = ?", (chat_id,))
    await db_conn.commit()

async def is_key_marked_invalid(chat_id: int) -> bool:
    async with db_conn.cursor() as cursor:
        await cursor.execute("SELECT invalid_key FROM users WHERE chat_id = ?", (chat_id,))
        row = await cursor.fetchone()
        return bool(row[0]) if row else False




#**********************************************
# 📁 Новые функции работы с данными
#**********************************************

# ✅ Получение данных пользователя
async def get_user_data(chat_id: Union[str, int]):
    chat_id = int(chat_id)
    async with db_conn.cursor() as cursor:
        # Получение данных пользователя
        await cursor.execute(
            "SELECT chat_id, trongrid_api_key FROM users WHERE chat_id = ?", 
            (chat_id,)
        )
        user_record = await cursor.fetchone()
        
        if not user_record:
            return None
        
        # Получение адресов
        await cursor.execute(
            "SELECT address, last_checked FROM addresses WHERE user_chat_id = ?", 
            (chat_id,)
        )
        address_records = await cursor.fetchall()
        
        addresses = {
            r[0]: {"last_checked": r[1]} 
            for r in address_records
        }
        
        return {
            "chat_id": str(user_record[0]),
            "addresses": addresses,
            "trongrid_api_key": user_record[1]
        }


# ✅ Гарантия существования пользователя
async def ensure_user_exists(chat_id: Union[str, int]):
    chat_id = int(chat_id)
    async with db_conn.cursor() as cursor:
        await cursor.execute(
            """
            INSERT OR IGNORE INTO users (chat_id, trongrid_api_key) 
            VALUES (?, NULL)
            """,
            (chat_id,)
        )
    await db_conn.commit()

# ✅ Добавление адреса
async def add_tron_address(chat_id: Union[str, int], new_address: str):
    chat_id = int(chat_id)
    try:
        async with db_conn.cursor() as cursor:
            # 1. Находим максимальный last_checked
            await cursor.execute("SELECT MAX(last_checked) FROM addresses")
            max_ts = await cursor.fetchone()
            # Устанавливаем max_ts + 1 или 0
            initial_last_checked = (max_ts[0] + 1) if max_ts and max_ts[0] is not None else 0

            # 2. Вставляем новый адрес
            await cursor.execute(
                """
                INSERT INTO addresses (user_chat_id, address, last_checked) 
                VALUES (?, ?, ?)
                """,
                (chat_id, new_address, initial_last_checked)
            )
        await db_conn.commit()
        return True # Успешно добавлен
    except aiosqlite.IntegrityError:
        # Ошибка, если уникальное ограничение (UNIQUE (user_chat_id, address)) нарушено
        return False 
    except Exception as e:
        # Другие ошибки
        print(f"Ошибка при добавлении адреса: {e}")
        return False


# ✅ Обновление last_checked 
async def update_last_checked(chat_id: Union[str, int], address: str, new_timestamp: int):
    chat_id = int(chat_id)
    async with db_conn.cursor() as cursor:
        # Обновляем last_checked, если только новый timestamp больше
        await cursor.execute(
            "UPDATE addresses SET last_checked = MAX(last_checked, ?) WHERE user_chat_id = ? AND address = ?", 
            (new_timestamp, chat_id, address)
        )
    # Атомарное сохранение (фиксируем изменение в БД)
    await db_conn.commit() 


# ✅ Обновление ключа (используется для установки и удаления)
async def set_trongrid_api_key(chat_id: Union[str, int], key: Union[str, None]):
    chat_id = int(chat_id)
    async with db_conn.cursor() as cursor:
        await cursor.execute(
            "UPDATE users SET trongrid_api_key = ? WHERE chat_id = ?", 
            (key, chat_id)
        )
    await db_conn.commit()


# ✅ Удаление всех адресов
async def delete_all_addresses(chat_id: Union[str, int]):
    chat_id = int(chat_id)
    async with db_conn.cursor() as cursor:
        await cursor.execute(
            "DELETE FROM addresses WHERE user_chat_id = ?", 
            (chat_id,)
        )
    await db_conn.commit()



#**********************************************
# постинг в группу админа
#**********************************************
async def post_admin_group(msg, chat_id, type_transactions):
    chat_id = int(chat_id)
    
    # Проверяем, находится ли chat_id в списке ADMIN_IDS
    if chat_id in ADMIN_IDS:
        if type_transactions == 1:
            await app.bot.send_message(admin_group, msg, parse_mode="Markdown", message_thread_id=thread_energy)
        elif type_transactions == 2:
            await app.bot.send_message(admin_group, msg, parse_mode="Markdown", message_thread_id=thread_trx)
        elif type_transactions == 3:
            await app.bot.send_message(admin_group, msg, parse_mode="Markdown", message_thread_id=thread_usdt)
        elif type_transactions == 4:
            await app.bot.send_message(admin_group, msg, parse_mode="Markdown", message_thread_id=thread_bw)
    return



#**********************************************
# 📌 Логирование ошибок
#**********************************************
def log_error_crash(message_crash):
    with open(CRASH_Energyfile, 'a') as log_file:
        log_file.write("\n" + "="*100 + "\n")
        current_time = (datetime.now() + timedelta(hours=timedelta_hours)).strftime("%Y-%m-%d %H:%M:%S")
        log_file.write(f"Дата-время: {current_time}\n{message_crash}\n")
        logging.info(f"Дата-время: {current_time}\n{message_crash}\n")
        

#**********************************************
# 🧮 Форматирование чисел
#**********************************************
def format_peremen(balance):
    return f"{balance:,}"



#**********************************************
# 🔍 Получение информации о ресурсах
#**********************************************

async def get_energy_info(owner_address, trongrid_key=None):

    try:
        payload = {"address": owner_address, "visible": True}
        # Используем существующую функцию для запроса к TronGrid
        data = await fetch_tron_post_with_rate_limit("/wallet/getaccountresource", payload, api_key=trongrid_key)

        # --- 1. Извлечение данных по Energy ---
        energy_used = data.get("EnergyUsed", 0)
        energy_limit = data.get("EnergyLimit", 0)
        total_energy_limit = data.get("TotalEnergyLimit", 0)
        total_energy_weight = data.get("TotalEnergyWeight", 0)

        # --- 2. Извлечение данных по Net (Bandwidth) ---
        # NetUsed = freeNetUsed + assetNetUsed (NetUsed от замороженных TRX)
        # NetLimit = freeNetLimit + assetNetLimit (NetLimit от замороженных TRX)
        net_used = data.get("NetUsed", 0)  # Total Net Used
        net_limit = data.get("NetLimit", 0)  # Total Net Limit
        total_net_limit = data.get("TotalNetLimit", 0)
        total_net_weight = data.get("TotalNetWeight", 0)

        # --- 3. Расчет цены Energy (стоимость 1 Energy в TRX) ---
        if total_energy_limit == 0:
            trx_energy_price = Decimal('0')
        else:
            # Расчет цены: TotalEnergyWeight / TotalEnergyLimit
            trx_energy_price = (Decimal(total_energy_weight) / Decimal(total_energy_limit))
            trx_energy_price = trx_energy_price.quantize(Decimal('0.00001'))

        # --- 4. Расчет цены Net (стоимость 1 Bandwidth в TRX) ---
        if total_net_limit == 0:
            trx_net_price = Decimal('0')
        else:
            # Расчет цены: TotalNetWeight / TotalNetLimit
            trx_net_price = (Decimal(total_net_weight) / Decimal(total_net_limit))
            trx_net_price = trx_net_price.quantize(Decimal('0.00001'))

        # --- 5. Расчет свободных ресурсов и "стоимости слота" (unused_slot) ---
        free_energy = energy_limit - energy_used
        unused_energy_trx = int(free_energy * trx_energy_price)

        free_net = net_limit - net_used
        unused_net_trx = int(free_net * trx_net_price)

        # Возвращаем кортеж с данными по Energy и Net
        return (
            free_energy, trx_energy_price, unused_energy_trx,
            free_net, trx_net_price, unused_net_trx
        )

    except Exception as e:
        log_error_crash(f"Ошибка в get_energy_and_net_info для {owner_address}: {str(e)}")
        # Возвращаем кортеж с соответствующим количеством None
        return None, None, None, None, None, None


#**********************************************
# 🔁 Конвертация hex → base58
#**********************************************
def hex_to_base58check(hex_address: str) -> str:
    if hex_address.startswith("41"):
        hex_body = bytes.fromhex(hex_address)
        sha256_1 = hashlib.sha256(hex_body).digest()
        sha256_2 = hashlib.sha256(sha256_1).digest()
        checksum = sha256_2[:4]
        address_bytes = hex_body + checksum
        return base58.b58encode(address_bytes).decode()
    return hex_address

#**********************************************
# Проверка адреса TRON
#**********************************************
def is_valid_tron_address(address):
    return isinstance(address, str) and address.startswith('T') and len(address) == 34

#**********************************************
# 🤖 Команды Telegram (ИСПРАВЛЕНО: Переход на aiosqlite)
#**********************************************
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # --- НОВЫЙ КОД ДЛЯ ПРОВЕРКИ ---
    current_threads = threading.active_count()
    current_thread_name = threading.current_thread().name
    active_tasks = len(asyncio.all_tasks())
    logging.info(f"📊 Активных потоков: {current_threads}. Текущий поток: {current_thread_name}")
    try:
        loop = asyncio.get_event_loop()
        active_tasks = len(asyncio.all_tasks(loop=loop))
        logging.info(f"📊 Активных асинхронных задач: {active_tasks}")
    except RuntimeError:
        # Если цикл не запущен (например, в начале программы)
        logging.info("📊 Не удалось получить активные асинхронные задачи (цикл не запущен).")
    # --- КОНЕЦ НОВОГО КОДА ---

    chat_id = str(update.effective_chat.id)
    
    await ensure_user_exists(chat_id)



    # Получаем текущие настройки
    settings = await get_monitoring_settings(int(chat_id))

    if await is_key_marked_invalid(int(chat_id)):
        await update.message.reply_text(
            "⚠️ Ваш TronGrid API-ключ недействителен или заблокирован.\n"
            "Пожалуйста, обновите его через меню: «➕ Добавить TronGrid API ключ»."
        )

    energy_text = "🔋 Энергия (вкл)" if settings["energy"] else "🔋 Энергия (выкл)"
    bw_text = "🔋 Бэндвич (вкл)" if settings["bw"] else "🔋 Бэндвич (выкл)"
    trx_text = "💰 TRX (вкл)" if settings["trx"] else "💰 TRX (выкл)"
    usdt_text = "💵 USDT (вкл)" if settings["usdt"] else "💵 USDT (выкл)"


    markup = ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить адрес TRON"), KeyboardButton("➕ Добавить TronGrid API ключ")],
            [KeyboardButton("📋 Список адресов TRON"), KeyboardButton("👁 Показать TronGrid API ключ")],
            [KeyboardButton("🗑 Удалить все адреса TRON"), KeyboardButton("🗑 Удалить TronGrid API ключ")],
            [KeyboardButton(energy_text), KeyboardButton(bw_text), KeyboardButton(trx_text), KeyboardButton(usdt_text)],
            [KeyboardButton("🚬 Синхронизация")]
        ],
        resize_keyboard=True
    )
    current_time_data = (datetime.now() + timedelta(hours=timedelta_hours)).strftime("%Y-%m-%d %H:%M:%S")
    description = (
        "👋 Добро пожаловать в бота отслеживания транзакций TRON!\n\n"
        "💡 Данный бот информирует вас о новых транзакциях на ваших кошельках в реальном времени.\n\n"
        "Вы будете получать уведомления о:\n"
        "▫ Исходящих и входящих делегациях энергии\n"
        "▫ Исходящих и входящих делегациях бэндвич\n"
        "▫ Исходящих и входящих переводах TRX\n"
        "▫ Исходящих и входящих переводах USDT\n\n"
        "🔗 [Тут можете ознакомиться с настройкой и описанием бота](https://t.me/TrxTronRU/6836/6837)\n\n"
        "📬 [Тут можете написать разработчику бота в личку](https://t.me/PostToMe_bot)\n\n"
        "Внимание! Для работы бота требуется API ключ [TronGrid](https://www.trongrid.io/dashboard/keys). По желанию можете добавить свой API ключ TronGrid, или использовать мой автоматический (по умолчанию).\n\n"
        f"Дата: {current_time_data}\n\n"
        "Выберите действие:"
    )
    await update.message.reply_text(description, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    
    # ИСПРАВЛЕНО: Убеждаемся, что пользователь есть в БД и загружаем данные из БД
    await ensure_user_exists(chat_id) 
    user_data = await get_user_data(chat_id) # Получаем актуальные данные из БД

    # Если user_data - None (что не должно произойти после ensure_user_exists, но для безопасности)
    if not user_data:
        await update.message.reply_text("Произошла ошибка при загрузке данных пользователя. Пожалуйста, попробуйте снова.")
        return

    # 🔄 СБРАСЫВАЕМ флаги ввода, если пользователь нажал ЛЮБУЮ кнопку меню
    menu_buttons = {
        "➕ Добавить адрес TRON",
        "➕ Добавить TronGrid API ключ",
        "📋 Список адресов TRON",
        "👁 Показать TronGrid API ключ",
        "🗑 Удалить все адреса TRON",
        "🗑 Удалить TronGrid API ключ",
        "🔋 Энергия (вкл)",
        "🔋 Энергия (выкл)",
        "🔋 Бэндвич (вкл)",
        "🔋 Бэндвич (выкл)",
        "💰 TRX (вкл)",
        "💰 TRX (выкл)",
        "💵 USDT (вкл)",
        "💵 USDT (выкл)",
        "🚬 Синхронизация"
    }


    if text in menu_buttons:
        # Сбрасываем все флаги ввода
        context.user_data.pop("adding_address", None)
        context.user_data.pop("adding_trongrid_key", None)


    adding_address = context.user_data.get("adding_address")
    adding_key = context.user_data.get("adding_trongrid_key")

    if text == "➕ Добавить адрес TRON":
        await update.message.reply_text("Введите адрес TRON для отслеживания:")
        context.user_data["adding_address"] = True
        context.user_data["adding_trongrid_key"] = False # Сброс других флагов

    elif text == "➕ Добавить TronGrid API ключ":
        await update.message.reply_text("Введите ваш TronGrid API ключ:")
        context.user_data["adding_trongrid_key"] = True
        context.user_data["adding_address"] = False # Сброс других флагов

    elif text == "🚬 Синхронизация":
        global scheduler # <-- Объявляем глобальный планировщик
        
        context.user_data["adding_trongrid_key"] = False
        context.user_data["adding_address"] = False

        # 1. Отправляем пользователю сообщение о начале операции
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="⌛️ Запущена синхронизация базы данных. Ожидайте..."
        )

        try:
            logging.info("⏸️ Приостановка планировщика перед WAL Checkpoint.")
            scheduler.pause()
            
            # Даем 5 секунд на завершение любых активных асинхронных задач (ридеров)
            await asyncio.sleep(5) 
            
            # 2. Вызываем синхронизацию (она выполнит PRAGMA wal_checkpoint(TRUNCATE))
            await flush_wal_to_db()
            
            # 3. Возобновляем работу планировщика
            scheduler.resume()
            logging.info("▶️ Планировщик возобновлен.")

            # 4. Отправляем финальное сообщение
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="✅ База данных успешно синхронизирована (WAL Checkpoint выполнен). Размер .wal файла должен быть сброшен."
            )

        except Exception as e:
            # Важно: В случае ошибки нужно возобновить планировщик
            if scheduler.running:
                scheduler.resume()
            log_error_crash(f"Ошибка при синхронизации базы данных: {e}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text="❌ Произошла ошибка при синхронизации базы данных. Попробуйте позже."
            )


    elif text == "🗑 Удалить все адреса TRON":
        # ИСПРАВЛЕНО: Используем асинхронную функцию удаления из БД
        await delete_all_addresses(chat_id)
        await update.message.reply_text("✅ Все адреса удалены.")
        await start(update, context)

    elif text == "🗑 Удалить TronGrid API ключ":
        # ИСПРАВЛЕНО: Используем асинхронную функцию установки ключа в NULL в БД
        await set_trongrid_api_key(chat_id, None)
        await update.message.reply_text("✅ TronGrid API ключ удален.")
        await start(update, context)

    elif text == "📋 Список адресов TRON":
        addresses = user_data.get("addresses", {})
        if addresses:
            msg = "📌 Ваши адреса:\n" + "\n".join(addresses.keys())
        else:
            msg = "⚠️ Вы не добавили ни одного адреса."
        await update.message.reply_text(msg)
        await start(update, context)

    elif text == "👁 Показать TronGrid API ключ":
        key = user_data.get("trongrid_api_key")
        if key:
            await update.message.reply_text(f"🔑 Ваш TronGrid API ключ:\n{key}")
            await start(update, context)
        else:
            await update.message.reply_text("⚠️ Вы не добавили TronGrid API ключ.")
            await start(update, context)

    elif adding_address:
        new_address = text
        context.user_data["adding_address"] = False # Сброс флага
        
        if not is_valid_tron_address(new_address):
            await update.message.reply_text("❌ Неверный формат TRON-адреса.")
            await start(update, context)
        else:
            # ИСПРАВЛЕНО: Используем асинхронную функцию добавления, которая сама обрабатывает last_checked и дубликаты
            success = await add_tron_address(chat_id, new_address)

            if success:
                await update.message.reply_text(f"✅ Адрес {new_address} добавлен.")
            else:
                # Проверяем, почему не удалось: скорее всего, дубликат
                if new_address in user_data.get("addresses", {}):
                     await update.message.reply_text("⚠️ Этот адрес уже добавлен.")
                else:
                    # Другая ошибка
                    await update.message.reply_text("❌ Произошла ошибка при добавлении адреса.")
            
            await start(update, context)


    elif adding_key:
        new_key = text.strip()
        context.user_data["adding_trongrid_key"] = False
        if await is_valid_trongrid_key(new_key):
            await set_trongrid_api_key(chat_id, new_key)
            await clear_invalid_key_flag(int(chat_id))
            await update.message.reply_text("✅ Ключ успешно сохранён и проверен.")
        else:
            await update.message.reply_text("❌ Недействительный или нерабочий TronGrid API-ключ. Попробуйте другой.")
        await start(update, context)


    # --- Обработка переключателей мониторинга ---
    elif text in ["🔋 Энергия (вкл)", "🔋 Энергия (выкл)"]:
        setting = "energy"
        enabled = text == "🔋 Энергия (вкл)"
        await toggle_monitoring(int(chat_id), setting, not enabled)  # переключаем на противоположное
        state = "включено" if not enabled else "отключено"
        await update.message.reply_text(f"✅ Отслеживание энергии {state}.")
        await start(update, context)

    elif text in ["🔋 Бэндвич (вкл)", "🔋 Бэндвич (выкл)"]:
        setting = "bw"
        enabled = text == "🔋 Бэндвич (вкл)"
        await toggle_monitoring(int(chat_id), setting, not enabled)  # переключаем на противоположное
        state = "включено" if not enabled else "отключено"
        await update.message.reply_text(f"✅ Отслеживание бэндвич {state}.")
        await start(update, context)



    elif text in ["💰 TRX (вкл)", "💰 TRX (выкл)"]:
        setting = "trx"
        enabled = text == "💰 TRX (вкл)"
        await toggle_monitoring(int(chat_id), setting, not enabled)
        state = "включено" if not enabled else "отключено"
        await update.message.reply_text(f"✅ Отслеживание TRX {state}.")
        await start(update, context)

    elif text in ["💵 USDT (вкл)", "💵 USDT (выкл)"]:
        setting = "usdt"
        enabled = text == "💵 USDT (вкл)"
        await toggle_monitoring(int(chat_id), setting, not enabled)
        state = "включено" if not enabled else "отключено"
        await update.message.reply_text(f"✅ Отслеживание USDT {state}.")
        await start(update, context)



    # --- Любое другое сообщение (не кнопка и не ввод) ---
#    else:
#        # Неизвестная команда — можно проигнорировать или дать подсказку
#        await update.message.reply_text("Пожалуйста, используйте кнопки меню.")
#        await start(update, context)





#**********************************************
# 🔍возвращает подписавшего транзакцию
# 🔒 Асинхронная блокировка для ограничения частоты запросов к TronScan API
# Используется, чтобы только один запрос txid_get_tronscan выполнялся в единицу времени.
TRONSCAN_LOCK = asyncio.Lock()
#**********************************************
async def txid_get_tronscan(tx_id: str) -> Union[str, None]:
    url = f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx_id}"

    # 1. Захватываем блокировку:
    # Другие задачи будут ждать здесь, пока блокировка не будет освобождена.
    async with TRONSCAN_LOCK: 
        
        try:
            # ⏸️ Пауза 1 секунда перед запросом к Tronscan
            # Пауза теперь находится внутри блокировки, что гарантирует
            # интервал между последовательными запросами, даже от разных задач.
            await asyncio.sleep(Pause_txid_get_tronscan)
            
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=15)

            if response.status_code != 200:
                # Ошибка HTTP
                return None

            data = response.json()
            signature_addresses = data.get("signature_addresses")

            if not signature_addresses or not isinstance(signature_addresses, list):
                log_error_crash(f"Ошибка получении адреса подписавшего транзакцию делегации энергии в блоке txid_get_tronscan")
                return None

            return signature_addresses[0]  # Возвращаем первый адрес

        except Exception as e:
            # Любая ошибка
            log_error_crash(f"Ошибка получении адреса подписавшего транзакцию делегации энергии в блоке txid_get_tronscan: {e}")
            return None
    
    # 2. Блокировка автоматически освобождается, когда мы выходим из блока 'async with




#**********************************************
# 🌐 API-запрос с контролем скорости (QPS)
#**********************************************
def get_key_semaphore(api_key: str):
    if api_key not in KEY_SEMAPHORES:
        KEY_SEMAPHORES[api_key] = {
            'semaphore': asyncio.Semaphore(1),
            'last_request_time': 0.0
        }
    return KEY_SEMAPHORES[api_key]


async def fetch_tron_data_with_rate_limit(address: str, endpoint: str, api_key: str = None) -> dict:
    """
    Выполняет запрос к TronGrid с контролем QPS.
    Если api_key указан — используется он.
    Иначе — берётся следующий ключ из глобального пула KEY_POOL.
    """
    # Выбор ключа
    if api_key and api_key.strip():
        key_to_use = api_key.strip()
    else:
        # Берём из глобального пула
        key_obj = await get_next_key_object()
        if not key_obj:
            raise Exception("Нет доступных API-ключей!")
        key_to_use = key_obj['key']

    # Получаем семафор и время для этого ключа
    key_info = get_key_semaphore(key_to_use)
    semaphore = key_info['semaphore']

    async with semaphore:
        current_time = time.time()
        time_since_last = current_time - key_info['last_request_time']
        if time_since_last < QPS_LIMIT_SECONDS:
            await asyncio.sleep(QPS_LIMIT_SECONDS - time_since_last)
        key_info['last_request_time'] = time.time()

        # Выполняем запрос
        url = f"https://api.trongrid.io{endpoint}"
        headers = {"TRON-PRO-API-KEY": key_to_use}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()



async def fetch_tron_post_with_rate_limit(endpoint: str, payload: dict, api_key: str = None) -> dict:
    if api_key and api_key.strip():
        key_to_use = api_key.strip()
    else:
        key_obj = await get_next_key_object()
        if not key_obj:
            raise Exception("Нет доступных API-ключей!")
        key_to_use = key_obj['key']

    key_info = get_key_semaphore(key_to_use)
    semaphore = key_info['semaphore']

    async with semaphore:
        current_time = time.time()
        time_since_last = current_time - key_info['last_request_time']
        if time_since_last < QPS_LIMIT_SECONDS:
            await asyncio.sleep(QPS_LIMIT_SECONDS - time_since_last)
        key_info['last_request_time'] = time.time()

        url = f"https://api.trongrid.io{endpoint}"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "TRON-PRO-API-KEY": key_to_use
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()





#**********************************************
# 🔍 Проверка транзакций делегации энергии (ИСПРАВЛЕНО: Удален некорректный finally)
#**********************************************
async def check_energy_delegations():

    start_time = time.time()

    # 1. Получаем ВСЕХ пользователей и их адреса одним запросом
    async with db_conn.cursor() as cursor:
        query = """
        SELECT 
            u.chat_id, u.trongrid_api_key, a.address, a.last_checked
        FROM users u
        JOIN addresses a ON u.chat_id = a.user_chat_id
        """
        # Используем fetchall() для получения всех данных
        all_data = await cursor.execute(query)
        all_rows = await all_data.fetchall()

    # 2. Группируем данные по пользователю
    users_to_process = {}
    for row in all_rows:
        chat_id_str = str(row[0]) # chat_id
        if chat_id_str not in users_to_process:
            users_to_process[chat_id_str] = {
                "chat_id": chat_id_str,
                "trongrid_api_key": row[1], # trongrid_api_key
                "addresses": {}
            }
        users_to_process[chat_id_str]["addresses"][row[2]] = { # address
            "last_checked": row[3] # last_checked
        }

    num_users = len(users_to_process)

    # --- ИСПРАВЛЕННЫЙ БЛОК ЛОГИРОВАНИЯ ---
    current_threads = threading.active_count()
    current_thread_name = threading.current_thread().name
    # 🔢 Считаем общее число кошельков (адресов)
    total_wallets = sum(len(data["addresses"]) for data in users_to_process.values())
    # Безопасно получаем активные асинхронные задачи.
    # Так как функция асинхронная, get_event_loop() не нужен.
    try:
        active_tasks = len(asyncio.all_tasks())
    except Exception:
        active_tasks = "N/A (Ошибка контекста asyncio)"

    logging.info("---------------------------------------")
    logging.info("🔎 СТАРТ ЦИКЛА ПРОВЕРКИ TRONGRID")
    logging.info(f"✅ Пользователей к конкурентной проверке: {num_users}")
    logging.info(f"👛 Всего кошельков (адресов) к проверке: {total_wallets}")
    logging.info(f"📊 Активных системных потоков: {current_threads}. Имя потока: {current_thread_name}")
    logging.info(f"⚡ Активных асинхронных задач (тасков): {active_tasks}")
    # --- КОНЕЦ ИСПРАВЛЕННОГО БЛОКА ---


    # 3. Запускаем асинхронные задачи обработки для каждого пользователя
    tasks = []
    for chat_id_str, data in users_to_process.items():
        tasks.append(process_user_transactions(data))
    await asyncio.gather(*tasks)
    end_time = time.time()
    duration = end_time - start_time
    logging.info("✅ ЦИКЛ ПРОВЕРКИ TRONGRID ЗАВЕРШЕН.")
    logging.info(f"⏱️ Общее время выполнения: {duration:.2f} секунд.")
    logging.info("---------------------------------------")

#**********************************************
# 🔄 Глобальная ротация ключа
#**********************************************
async def get_next_key_object():
    """
    Возвращает следующий объект ключа из пула в режиме round-robin.
    Содержит ключ, его семафор и время последнего запроса.
    """
    global key_pool_index
    
    if not KEY_POOL:
        return None

    # Ротация происходит равномерно между доступными ключами
    key_object = KEY_POOL[key_pool_index % len(KEY_POOL)]
    key_pool_index += 1
    
    return key_object



async def process_user_transactions(user_data):
    chat_id_str = user_data["chat_id"]
    chat_id = int(chat_id_str)
    user_api_key = user_data.get("trongrid_api_key")  # Может быть None

    # 🔹 Получаем настройки мониторинга пользователя
    settings = await get_monitoring_settings(chat_id)
    monitor_energy = settings["energy"]
    monitor_trx = settings["trx"]
    monitor_usdt = settings["usdt"]
    monitor_bw = settings["bw"]

    # NOTE: Желательно добавить monitor_bandwidth = settings["bandwidth"], 
    # но используем monitor_energy, как вы просили.

    try:
        for address, addr_data in user_data.get("addresses", {}).items():
            last_checked = int(addr_data.get("last_checked", 0))
            new_last_timestamp = last_checked

            # 🔸 1) Проверка транзакций энергии, TRX и BW (только если включено)
            if monitor_energy or monitor_trx or monitor_bw:
                try:
                    min_block_ts = last_checked + 1
                    endpoint = (
                        f"/v1/accounts/{address}/transactions"
                        f"?limit={limit_txhd}&order_by=block_timestamp,asc&min_block_timestamp={min_block_ts}"
                    )
                    tx_data = await fetch_tron_data_with_rate_limit(address, endpoint, api_key=user_api_key)
                    transactions = tx_data.get("data", [])

                    # 🔑 ИЗМЕНЕНИЕ 1: Добавляем переменные Bandwidth при вызове
                    free_energy, trx_energy_price, unused_slot_energy, \
                    free_bw, trx_bw_price, unused_slot_bw = await get_energy_info(address, trongrid_key=user_api_key)

                    for tx in sorted(transactions, key=lambda x: x.get("block_timestamp", 0)):
                        tx_timestamp = int(tx.get("block_timestamp", 0))
                        if tx_timestamp <= last_checked:
                            continue

                        tx_id = tx["txID"]
                        contract_type = tx["raw_data"]["contract"][0]["type"]
                        tx_link = f"https://tronscan.org/#/transaction/{tx_id}"
                        date = (datetime.fromtimestamp(tx_timestamp / 1000) + timedelta(hours=timedelta_hours)).strftime("%Y-%m-%d %H:%M:%S")

                        
                        if contract_type == "DelegateResourceContract" and (monitor_energy or monitor_bw): # ИСПОЛЬЗУЕМ ДЛЯ  ПРОВЕРОК
                            contract = tx["raw_data"]["contract"][0]["parameter"]["value"]
                            resource_type = contract.get("resource", "") # Ключевой параметр для разделения

                            from_hex = contract.get("owner_address", "")
                            to_hex = contract.get("receiver_address", "")
                            from_address = hex_to_base58check(from_hex)
                            to_address = hex_to_base58check(to_hex)
                            deleg_in_trx = int(contract.get("balance", 0) / 1_000_000)
                            signer_address = await txid_get_tronscan(tx_id)
                            signer_address_text = f"Подписал: {hash_hash}{signer_address}" if signer_address else "Подписал: None"


                            # --- БЛОК ПРОВЕРКИ ENERGY (СТАРЫЙ) ---
                            if resource_type == "ENERGY" and monitor_energy:
                                if trx_energy_price and trx_energy_price > 0:
                                    deleg_in_energy = int(Decimal(deleg_in_trx) / trx_energy_price)
                                else:
                                    deleg_in_energy = 0
                                
                                # Определяем направление и формируем сообщение
                                direction = "исходящая" if from_address == address else "входящая"
                                hashtag = "ENERGYOut" if from_address == address else "ENERGYIn"
                                if from_address == address or to_address == address:
                                    msg = (
                                        f"🔋 Новая {direction} делегация ЭНЕРГИИ:\n"
                                        f"▪ От: {hash_hash}{from_address}\n"
                                        f"▫ Кому: {hash_hash}{to_address}\n"
                                        f"▫ Сумма: {format_peremen(deleg_in_energy)} Energy\n"
                                        f"▫ Эквивалент: {format_peremen(deleg_in_trx)} TRX\n"
                                        f"▫ Остаток: {format_peremen(unused_slot_energy)} TRX | {format_peremen(free_energy)} Energy\n"
                                        f"▫ {signer_address_text}\n"
                                        f"▫ Дата: {date}\n"
                                        f"▫ [Просмотр транзакции]({tx_link})\n"
                                        f"▫ Хештег: {hash_hash}{hashtag}"
                                    )
                                    await post_admin_group(msg, chat_id, 1)
                                    await app.bot.send_message(chat_id, msg, parse_mode="Markdown")
                                
                            
                            # 🚀 --- БЛОК ПРОВЕРКИ BANDWIDTH (НОВЫЙ) --- 🚀
                            elif resource_type == "" and monitor_bw: # bw без ресурса обозначается
                                if trx_bw_price and trx_bw_price > 0:
                                    deleg_in_bw = int(Decimal(deleg_in_trx) / trx_bw_price)
                                else:
                                    deleg_in_bw = 0
                                
                                # Определяем направление и формируем сообщение
                                direction = "исходящая" if from_address == address else "входящая"
                                hashtag = "BANDWIDTHOut" if from_address == address else "BANDWIDTHIn"
                                if from_address == address or to_address == address:
                                    msg = (
                                        f"📶 Новая {direction} делегация БЭНДВИЧ:\n"
                                        f"▪ От: {hash_hash}{from_address}\n"
                                        f"▫ Кому: {hash_hash}{to_address}\n"
                                        f"▫ Сумма: {format_peremen(deleg_in_bw)} Bandwidth\n"
                                        f"▫ Эквивалент: {format_peremen(deleg_in_trx)} TRX\n"
                                        f"▫ Остаток: {format_peremen(unused_slot_bw)} TRX | {format_peremen(free_bw)} Bandwidth\n"
                                        f"▫ {signer_address_text}\n"
                                        f"▫ Дата: {date}\n"
                                        f"▫ [Просмотр транзакции]({tx_link})\n"
                                        f"▫ Хештег: {hash_hash}{hashtag}"
                                    )
                                    await post_admin_group(msg, chat_id, 4) # постим в тред bw 
                                    await app.bot.send_message(chat_id, msg, parse_mode="Markdown")
                            # --- КОНЕЦ БЛОКА BANDWIDTH ---
                            
                            else:
                                # Контракт DelegateResourceContract, но не Energy и не Bandwidth (такое маловероятно)
                                continue
                        
                        
                        elif contract_type == "TransferContract" and monitor_trx:
                            # ... (Ваша логика TransferContract (TRX) остается без изменений) ...
                            contract = tx["raw_data"]["contract"][0]["parameter"]["value"]
                            from_hex = contract.get("owner_address", "")
                            to_hex = contract.get("to_address", "")
                            amount = contract.get("amount", 0) / 1_000_000
                            from_address = hex_to_base58check(from_hex)
                            to_address = hex_to_base58check(to_hex)

                            if amount > 0.1:
                                direction = "Исходящий" if from_address == address else "Входящий"
                                hashtag = "TRXOut" if from_address == address else "TRXIn"

                                if from_address == address or to_address == address:
                                    msg = (
                                        f"📥 {direction} перевод TRX:\n"
                                        f"▪ От: {hash_hash}{from_address}\n"
                                        f"▫ Кому: {hash_hash}{to_address}\n"
                                        f"▫ Сумма: {format_peremen(amount)} TRX\n"
                                        f"▫ Дата: {date}\n"
                                        f"▫ [Просмотр транзакции]({tx_link})\n"
                                        f"▫ Хештег: {hash_hash}{hashtag}"
                                    )
                                    await post_admin_group(msg, chat_id, 2)
                                    await app.bot.send_message(chat_id, msg, parse_mode="Markdown")
                                else:
                                    continue
                        # ... (Конец логики TransferContract) ...
                        
                        
                        # Обновление метки времени
                        if tx_timestamp > new_last_timestamp:
                            new_last_timestamp = tx_timestamp

                except Exception as e:
                    error_msg = str(e).lower()
                    
                    # 🚀 ПРИОРИТЕТНАЯ ПРОВЕРКА: Если пользователь ЗАБЛОКИРОВАЛ бота
                    if "bot was blocked by the user" in error_msg:
                        # Выполняем необратимое действие - удаление данных
                        await delete_user_wallets_and_data(chat_id) 
                    
                        # Логгируем, что данные удалены, и выходим из цикла/функции, чтобы не продолжать проверку
                        log_error_crash(f"Пользователь {chat_id} заблокировал бота. Данные и кошельки УДАЛЕНЫ.")
                        return  # Прерываем выполнение, чтобы не выполнять mark_key_as_invalid
                
                    # ⚙️ ОБЫЧНАЯ ЛОГИКА: Обработка всех остальных ошибок 403/401/Unauthorized/Forbidden
                    elif user_api_key and ("403" in error_msg or "401" in error_msg or "unauthorized" in error_msg or "forbidden" in error_msg):
                        # Выполняем стандартное действие для других критических ошибок API
                        await mark_key_as_invalid(chat_id)
                        log_error_crash(f"Ключ пользователя {chat_id} помечен как недействительный: {e}")
                        if user_api_key and ("403" in error_msg or "401" in error_msg or "unauthorized" in error_msg or "forbidden" in error_msg):
                            await mark_key_as_invalid(chat_id)
                            log_error_crash(f"Ключ пользователя {chat_id} помечен как недействительный: {e}")
                    else:
                        log_error_crash(f"Ошибка проверки TRX/Energy/BW {address}: {e}")

            # 🔸 2) Проверка транзакций USDT (только если включено)
            # ... (Этот блок остается без изменений) ...
            if monitor_usdt:
                try:
                    min_block_ts = last_checked + 1
                    trc20_endpoint = (
                        f"/v1/accounts/{address}/transactions/trc20"
                        f"?limit={limit_txhd}&order_by=block_timestamp,asc&min_block_timestamp={min_block_ts}"
                    )
                    trc20_data = await fetch_tron_data_with_rate_limit(address, trc20_endpoint, api_key=user_api_key)
                    trc20_transactions = trc20_data.get("data", [])

                    for tx in sorted(trc20_transactions, key=lambda x: x.get("block_timestamp", 0)):
                        tx_timestamp = int(tx.get("block_timestamp", 0))
                        if tx_timestamp <= last_checked:
                            continue

                        tx_id = tx["transaction_id"]
                        tx_link = f"https://tronscan.org/#/transaction/{tx_id}"
                        date = (datetime.fromtimestamp(tx_timestamp / 1000) + timedelta(hours=timedelta_hours)).strftime("%Y-%m-%d %H:%M:%S")
                        contract_address = str(tx.get("token_info", {}).get("address", "")).lower()

                        if contract_address != USDT_CONTRACT_ADDRESS:
                            continue

                        amount_int = int(tx.get("value", 0))
                        amount_usdt = amount_int / 1_000_000
                        from_address = tx.get("from")
                        to_address = tx.get("to")

                        if amount_usdt < 0.1:
                            continue

                        direction = "Исходящий" if from_address == address else "Входящий"
                        hashtag = "USDTOut" if from_address == address else "USDTIn"
                        
                        if from_address == address or to_address == address:
                            msg = (
                                f"💸 {direction} перевод USDT:\n"
                                f"▪ От: {hash_hash}{from_address}\n"
                                f"▫ Кому: {hash_hash}{to_address}\n"
                                f"▫ Сумма: {format_peremen(amount_usdt)} USDT\n"
                                f"▫ Дата: {date}\n"
                                f"▫ [Просмотр транзакции]({tx_link})\n"
                                f"▫ Хештег: {hash_hash}{hashtag}"
                            )

                            await post_admin_group(msg, chat_id, 3)
                            await app.bot.send_message(chat_id, msg, parse_mode="Markdown")
                        else:
                            continue

                        if tx_timestamp > new_last_timestamp:
                            new_last_timestamp = tx_timestamp

                except Exception as e:
                    error_msg = str(e).lower()
                    
                    # 🚀 ПРИОРИТЕТНАЯ ПРОВЕРКА: Если пользователь ЗАБЛОКИРОВАЛ бота
                    if "bot was blocked by the user" in error_msg:
                        # Выполняем необратимое действие - удаление данных
                        await delete_user_wallets_and_data(chat_id) 
                    
                        # Логгируем, что данные удалены, и выходим из цикла/функции, чтобы не продолжать проверку
                        log_error_crash(f"Пользователь {chat_id} заблокировал бота. Данные и кошельки УДАЛЕНЫ.")
                        return  # Прерываем выполнение, чтобы не выполнять mark_key_as_invalid
                
                    # ⚙️ ОБЫЧНАЯ ЛОГИКА: Обработка всех остальных ошибок 403/401/Unauthorized/Forbidden
                    elif user_api_key and ("403" in error_msg or "401" in error_msg or "unauthorized" in error_msg or "forbidden" in error_msg):
                        # Выполняем стандартное действие для других критических ошибок API
                        await mark_key_as_invalid(chat_id)
                        log_error_crash(f"Ключ пользователя {chat_id} помечен как недействительный: {e}")
                        if user_api_key and ("403" in error_msg or "401" in error_msg or "unauthorized" in error_msg or "forbidden" in error_msg):
                            await mark_key_as_invalid(chat_id)
                            log_error_crash(f"Ключ пользователя {chat_id} помечен как недействительный: {e}")
                    else:
                        log_error_crash(f"Ошибка проверки USDT TRC20 {address}: {e}")


            # 🔹 Обновляем last_checked только если были новые транзакции
            if new_last_timestamp > last_checked:
                await update_last_checked(chat_id, address, new_last_timestamp + 1)

    except Exception as e:
        log_error_crash(f"Ошибка пользователя {chat_id_str}: {str(e)}")



#**********************************************
# 🚀 Запуск бота
#**********************************************
if __name__ == '__main__':
    import nest_asyncio
    nest_asyncio.apply()

    async def main():
        global app, scheduler
        await flush_wal_to_db()
        await init_db()  # Инициализация базы данных

        # Создаем Telegram-бота
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        # 🔄 Планировщик проверок транзакций
        scheduler = AsyncIOScheduler()
        scheduler.add_job(check_energy_delegations, "interval", seconds=CHECK_INTERVAL_SECONDS, coalesce=True, max_instances=1)
        scheduler.start()

        logging.info("🟢 Бот и планировщик запущены в одном процессе.")
        await app.run_polling(close_loop=False)

    asyncio.run(main())

