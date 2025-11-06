import sqlite3
from typing import List, Dict, Tuple

DATABASE_FILE = "user_data.db"



def fetch_data(conn: sqlite3.Connection, table_name: str, columns: List[str]) -> Tuple[List[str], List[Dict]]:
    try:
        cursor = conn.cursor()
        column_list = ", ".join(columns)
        query = f"SELECT {column_list} FROM {table_name}"
        cursor.execute(query)
        rows = cursor.fetchall()
        column_names = [description[0] for description in cursor.description]
        data_dicts = [dict(zip(column_names, row)) for row in rows]
        return column_names, data_dicts
    except sqlite3.Error as e:
        print(f"❌ Ошибка при чтении таблицы {table_name}: {e}")
        return [], []


def read_db(db_path: str):
    print(f"🔗 Подключение к базе данных: {db_path}\n")
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 🔧 Автоматическая миграция: добавляем недостающие колонки
        cursor.execute("PRAGMA table_info(users)")
        columns = {col[1] for col in cursor.fetchall()}
        if "monitor_energy" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN monitor_energy BOOLEAN DEFAULT 1")
        if "monitor_trx" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN monitor_trx BOOLEAN DEFAULT 1")
        if "monitor_usdt" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN monitor_usdt BOOLEAN DEFAULT 1")
        if "invalid_key" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN invalid_key BOOLEAN DEFAULT 0")
        if "monitor_bw" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN monitor_bw BOOLEAN DEFAULT 1")

        cursor.execute("""
            UPDATE users SET
                monitor_energy = COALESCE(monitor_energy, 1),
                monitor_trx = COALESCE(monitor_trx, 1),
                monitor_usdt = COALESCE(monitor_usdt, 1),
                invalid_key = COALESCE(invalid_key, 0),
                monitor_bw = COALESCE(monitor_bw, 1)
        """)
        conn.commit()

        # --- Чтение таблицы USERS ---
        print("=" * 90)
        print("👤 Данные пользователей (Таблица 'users'):")
        print("=" * 90)

        cursor.execute("PRAGMA table_info(users)")
        user_columns = [col[1] for col in cursor.fetchall()]
        if not user_columns:
            print("— Таблица 'users' не существует.")
        else:
            _, user_data = fetch_data(conn, "users", user_columns)
            if user_data:
                col_widths = {name: len(name) for name in user_columns}
                for user in user_data:
                    for name in user_columns:
                        val = str(user.get(name)) if user.get(name) is not None else "NULL"
                        col_widths[name] = max(col_widths[name], len(val))
                header = " | ".join(name.ljust(col_widths[name]) for name in user_columns)
                print(header)
                print("-" * len(header))
                for user in user_data:
                    row = " | ".join(
                        (str(user.get(name)) if user.get(name) is not None else "NULL").ljust(col_widths[name])
                        for name in user_columns
                    )
                    print(row)
            else:
                print("— В таблице 'users' нет записей.")

        # --- Чтение таблицы ADDRESSES ---
        print("\n\n" + "=" * 90)
        print("💳 Отслеживаемые адреса (Таблица 'addresses'):")
        print("=" * 90)

        cursor.execute("PRAGMA table_info(addresses)")
        addr_columns = [col[1] for col in cursor.fetchall()]
        if addr_columns:
            _, addr_data = fetch_data(conn, "addresses", addr_columns)
            if addr_data:
                col_widths = {name: len(name) for name in addr_columns}
                for addr in addr_data:
                    for name in addr_columns:
                        val = str(addr.get(name)) if addr.get(name) is not None else "NULL"
                        col_widths[name] = max(col_widths[name], len(val))
                header = " | ".join(name.ljust(col_widths[name]) for name in addr_columns)
                print(header)
                print("-" * len(header))
                for addr in addr_data:
                    row = " | ".join(
                        (str(addr.get(name)) if addr.get(name) is not None else "NULL").ljust(col_widths[name])
                        for name in addr_columns
                    )
                    print(row)
            else:
                print("— В таблице 'addresses' нет записей.")
        else:
            print("— Таблица 'addresses' не существует.")

    except sqlite3.Error as e:
        print(f"❌ Критическая ошибка: {e}")
    finally:
        if conn:
            conn.close()
            print("\n🔗 Соединение с БД закрыто.")


if __name__ == "__main__":
    read_db(DATABASE_FILE)
