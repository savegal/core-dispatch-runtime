"""
Скрипт для резервного копирования базы данных.
Можно запускать вручную или через cron.
"""
import sys
import os
import shutil
import logging
from datetime import datetime
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("backup_db")

settings = get_settings()


def backup_database(backup_dir: str | Path | None = None, keep_backups: int = 7) -> bool:
    """
    Создает резервную копию базы данных.
    
    Args:
        backup_dir: Директория для сохранения бэкапов (по умолчанию "backups/")
        keep_backups: Количество бэкапов для хранения (удаляются самые старые)
    
    Returns:
        True если бэкап успешно создан, False в противном случае
    """
    try:
        # Получаем путь к БД
        db_url = settings.DB_URL
        
        if not db_url.startswith("sqlite:///"):
            logger.warning(f"Backup supports SQLite only. Current DB_URL: {db_url}")
            logger.warning("For PostgreSQL, use pg_dump manually or configure automated backups.")
            return False
        
        db_path = Path(db_url.replace("sqlite:///", ""))
        
        if not db_path.exists():
            logger.error(f"Database file not found: {db_path}")
            return False
        
        # Определяем директорию для бэкапов
        if backup_dir is None:
            backup_dir = Path("backups")
        else:
            backup_dir = Path(backup_dir)
        
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        # Создаем имя файла бэкапа с timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"db_backup_{timestamp}.db"
        backup_path = backup_dir / backup_filename
        
        # Копируем файл БД
        logger.info(f"Creating backup: {backup_path}")
        shutil.copy2(db_path, backup_path)
        
        # Проверяем, что файл скопирован
        if not backup_path.exists():
            logger.error(f"Backup file was not created: {backup_path}")
            return False
        
        backup_size_mb = backup_path.stat().st_size / (1024 * 1024)
        logger.info(f"✅ Backup created successfully: {backup_path} ({backup_size_mb:.2f} MB)")
        
        # Удаляем старые бэкапы (оставляем только последние keep_backups)
        cleanup_old_backups(backup_dir, keep_backups)
        
        return True
        
    except Exception as e:
        logger.error(f"Error creating backup: {e}", exc_info=True)
        return False


def cleanup_old_backups(backup_dir: Path, keep_count: int):
    """
    Удаляет старые бэкапы, оставляя только последние keep_count.
    
    Args:
        backup_dir: Директория с бэкапами
        keep_count: Количество бэкапов для сохранения
    """
    try:
        # Получаем все файлы бэкапов
        backup_files = list(backup_dir.glob("db_backup_*.db"))
        
        if len(backup_files) <= keep_count:
            return
        
        # Сортируем по времени модификации (старые первыми)
        backup_files.sort(key=lambda f: f.stat().st_mtime)
        
        # Удаляем самые старые
        to_delete = backup_files[:-keep_count]
        
        for backup_file in to_delete:
            try:
                backup_file.unlink()
                logger.info(f"Deleted old backup: {backup_file.name}")
            except Exception as e:
                logger.warning(f"Error deleting old backup {backup_file}: {e}")
        
        logger.info(f"Cleaned up {len(to_delete)} old backup(s), keeping {keep_count} most recent")
        
    except Exception as e:
        logger.warning(f"Error cleaning up old backups: {e}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Backup database")
    parser.add_argument(
        "--backup-dir",
        type=str,
        default=None,
        help="Directory for backups (default: backups/)"
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=7,
        help="Number of backups to keep (default: 7)"
    )
    
    args = parser.parse_args()
    
    success = backup_database(
        backup_dir=args.backup_dir,
        keep_backups=args.keep
    )
    
    sys.exit(0 if success else 1)



