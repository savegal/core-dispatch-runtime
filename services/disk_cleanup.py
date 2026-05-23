"""
Сервис для очистки старых файлов (скриншоты, логи).
Предотвращает переполнение диска.
"""
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from config.settings import get_settings

logger = logging.getLogger("services.disk_cleanup")
settings = get_settings()


def cleanup_old_files(
    directory: Path | str,
    max_age_days: int = 7,
    max_size_mb: int = 1000,
    pattern: str = "*"
) -> dict:
    """
    Удаляет старые файлы из директории.
    
    Args:
        directory: Путь к директории для очистки
        max_age_days: Максимальный возраст файлов в днях (удаляются файлы старше)
        max_size_mb: Максимальный размер директории в MB (если превышен, удаляются самые старые)
        pattern: Паттерн для поиска файлов (например, "*.png", "*.log")
    
    Returns:
        dict: Статистика очистки
    """
    directory = Path(directory)
    
    if not directory.exists():
        logger.warning(f"Directory {directory} does not exist, skipping cleanup")
        return {"deleted": 0, "freed_mb": 0, "errors": 0}
    
    deleted_count = 0
    freed_bytes = 0
    error_count = 0
    
    try:
        # Получаем все файлы, соответствующие паттерну
        files = list(directory.glob(pattern))
        
        if not files:
            logger.debug(f"No files found in {directory} matching pattern {pattern}")
            return {"deleted": 0, "freed_mb": 0, "errors": 0}
        
        # Вычисляем пороговую дату
        threshold_date = datetime.now() - timedelta(days=max_age_days)
        
        # Удаляем файлы старше max_age_days
        for file_path in files:
            try:
                if not file_path.is_file():
                    continue
                
                # Проверяем возраст файла
                file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                
                if file_mtime < threshold_date:
                    file_size = file_path.stat().st_size
                    file_path.unlink()
                    deleted_count += 1
                    freed_bytes += file_size
                    logger.debug(f"Deleted old file: {file_path} (age: {(datetime.now() - file_mtime).days} days)")
            except Exception as e:
                error_count += 1
                logger.warning(f"Error deleting file {file_path}: {e}")
        
        # Если размер директории все еще превышает max_size_mb, удаляем самые старые файлы
        current_size_mb = sum(f.stat().st_size for f in directory.glob(pattern) if f.is_file()) / (1024 * 1024)
        
        if current_size_mb > max_size_mb:
            logger.info(f"Directory {directory} size ({current_size_mb:.2f} MB) exceeds limit ({max_size_mb} MB), cleaning oldest files...")
            
            # Сортируем файлы по времени модификации (старые первыми)
            remaining_files = [
                (f, datetime.fromtimestamp(f.stat().st_mtime))
                for f in directory.glob(pattern)
                if f.is_file()
            ]
            remaining_files.sort(key=lambda x: x[1])
            
            # Удаляем самые старые файлы пока размер не станет нормальным
            for file_path, _ in remaining_files:
                if current_size_mb <= max_size_mb:
                    break
                
                try:
                    file_size = file_path.stat().st_size
                    file_path.unlink()
                    deleted_count += 1
                    freed_bytes += file_size
                    current_size_mb -= file_size / (1024 * 1024)
                    logger.debug(f"Deleted file to free space: {file_path}")
                except Exception as e:
                    error_count += 1
                    logger.warning(f"Error deleting file {file_path}: {e}")
        
        freed_mb = freed_bytes / (1024 * 1024)
        
        if deleted_count > 0:
            logger.info(
                f"Cleanup completed for {directory}: "
                f"deleted {deleted_count} files, freed {freed_mb:.2f} MB, {error_count} errors"
            )
        
        return {
            "deleted": deleted_count,
            "freed_mb": round(freed_mb, 2),
            "errors": error_count
        }
        
    except Exception as e:
        logger.error(f"Error during cleanup of {directory}: {e}", exc_info=True)
        return {"deleted": 0, "freed_mb": 0, "errors": 1}


def cleanup_screenshots(max_age_days: int = 7, max_size_mb: int = 500) -> dict:
    """
    Очищает старые скриншоты из директории screenshots.
    
    Args:
        max_age_days: Максимальный возраст скриншотов в днях
        max_size_mb: Максимальный размер директории в MB
    
    Returns:
        dict: Статистика очистки
    """
    screenshots_dir = Path(settings.PLAYWRIGHT_SCREENSHOTS_DIR)
    
    # Если путь относительный, делаем абсолютным
    if not screenshots_dir.is_absolute():
        screenshots_dir = Path.cwd() / screenshots_dir
    
    return cleanup_old_files(
        directory=screenshots_dir,
        max_age_days=max_age_days,
        max_size_mb=max_size_mb,
        pattern="*.png"
    )


def cleanup_logs(max_age_days: int = 30, max_size_mb: int = 1000) -> dict:
    """
    Очищает старые логи (если логирование в файлы включено).
    
    Args:
        max_age_days: Максимальный возраст логов в днях
        max_size_mb: Максимальный размер директории логов в MB
    
    Returns:
        dict: Статистика очистки
    """
    # Логи обычно находятся в директории logs/ или в корне проекта
    log_dirs = [
        Path("logs"),
        Path.cwd() / "logs"
    ]
    
    total_stats = {"deleted": 0, "freed_mb": 0, "errors": 0}
    
    for log_dir in log_dirs:
        if log_dir.exists():
            stats = cleanup_old_files(
                directory=log_dir,
                max_age_days=max_age_days,
                max_size_mb=max_size_mb,
                pattern="*.log"
            )
            total_stats["deleted"] += stats["deleted"]
            total_stats["freed_mb"] += stats["freed_mb"]
            total_stats["errors"] += stats["errors"]
    
    return total_stats


def cleanup_diagnostics(max_age_days: int = 7, max_size_mb: int = 1000) -> dict:
    """
    ИСПРАВЛЕНО: Очищает старые диагностические файлы из директории diagnostics/.
    
    Диагностические файлы (PNG скриншоты и HTML) создаются при ошибках для отладки.
    Они могут занимать много места, поэтому нужна автоматическая очистка.
    
    Args:
        max_age_days: Максимальный возраст файлов в днях (по умолчанию 7 дней)
        max_size_mb: Максимальный размер директории в MB (если превышен, удаляются самые старые)
    
    Returns:
        dict: Статистика очистки {"deleted": int, "freed_mb": float, "errors": int}
    """
    diagnostics_dir = Path("diagnostics")
    
    # Если путь относительный, делаем абсолютным
    if not diagnostics_dir.is_absolute():
        diagnostics_dir = Path.cwd() / diagnostics_dir
    
    # Очищаем PNG файлы
    png_stats = cleanup_old_files(
        directory=diagnostics_dir,
        max_age_days=max_age_days,
        max_size_mb=max_size_mb,
        pattern="*.png"
    )
    
    # Очищаем HTML файлы
    html_stats = cleanup_old_files(
        directory=diagnostics_dir,
        max_age_days=max_age_days,
        max_size_mb=max_size_mb,
        pattern="*.html"
    )
    
    # Объединяем статистику
    total_stats = {
        "deleted": png_stats["deleted"] + html_stats["deleted"],
        "freed_mb": round(png_stats["freed_mb"] + html_stats["freed_mb"], 2),
        "errors": png_stats["errors"] + html_stats["errors"]
    }
    
    if total_stats["deleted"] > 0:
        logger.info(
            f"Diagnostics cleanup: deleted {total_stats['deleted']} files "
            f"({png_stats['deleted']} PNG, {html_stats['deleted']} HTML), "
            f"freed {total_stats['freed_mb']} MB"
        )
    
    return total_stats


def cleanup_all(max_age_days: int = 7) -> dict:
    """
    ИСПРАВЛЕНО: Очищает все типы файлов (screenshots, logs, diagnostics).
    
    Удобная функция для периодической очистки всех артефактов.
    
    Args:
        max_age_days: Максимальный возраст файлов в днях
    
    Returns:
        dict: Общая статистика очистки
    """
    screenshots_stats = cleanup_screenshots(max_age_days=max_age_days)
    logs_stats = cleanup_logs(max_age_days=30)  # Логи храним дольше
    diagnostics_stats = cleanup_diagnostics(max_age_days=max_age_days)
    
    total_stats = {
        "deleted": screenshots_stats["deleted"] + logs_stats["deleted"] + diagnostics_stats["deleted"],
        "freed_mb": round(
            screenshots_stats["freed_mb"] + logs_stats["freed_mb"] + diagnostics_stats["freed_mb"],
            2
        ),
        "errors": screenshots_stats["errors"] + logs_stats["errors"] + diagnostics_stats["errors"]
    }
    
    logger.info(
        f"Total cleanup: deleted {total_stats['deleted']} files, "
        f"freed {total_stats['freed_mb']} MB, {total_stats['errors']} errors"
    )
    
    return total_stats

