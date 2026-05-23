"""
Скрипт для добавления колонки cookie_file в таблицу accounts.
Запустите этот скрипт один раз для обновления базы данных.
"""
import sqlite3
import sys
from pathlib import Path

# Получаем путь к БД из настроек
try:
    from config.settings import get_settings
    settings = get_settings()
    db_url = settings.DB_URL
    
    # Извлекаем путь к файлу из sqlite:///path/to/file.db
    if db_url.startswith("sqlite:///"):
        db_path = db_url.replace("sqlite:///", "")
    else:
        print(f"❌ Не поддерживаемый формат DB_URL: {db_url}")
        sys.exit(1)
except Exception as e:
    print(f"❌ Ошибка загрузки настроек: {e}")
    # Пробуем стандартный путь
    db_path = Path(__file__).parent.parent / "dev.db"

def main():
    db_file = Path(db_path)
    if not db_file.exists():
        print(f"❌ База данных не найдена: {db_file}")
        print("Создайте базу данных через: python -c 'from db.database import init_db; init_db()'")
        sys.exit(1)
    
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    
    try:
        # Проверяем, существует ли колонка
        cursor.execute("PRAGMA table_info(accounts)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if "cookie_file" in columns:
            print("✅ Колонка cookie_file уже существует")
            return
        
        # Добавляем колонку
        print(f"Добавляем колонку cookie_file в {db_file}...")
        cursor.execute("ALTER TABLE accounts ADD COLUMN cookie_file TEXT")
        conn.commit()
        
        print("✅ Колонка cookie_file успешно добавлена")
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()

