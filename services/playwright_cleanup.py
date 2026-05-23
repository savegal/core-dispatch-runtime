"""
ИСПРАВЛЕНО: Очистка временных файлов Playwright для предотвращения переполнения диска.

КРИТИЧНО: Playwright создает временные профили и кэш в /tmp (Linux) или %TEMP% (Windows).
При работе 24/7 эти файлы накапливаются и могут переполнить диск.
"""
import logging
import shutil
from pathlib import Path
from typing import List
from config.settings import get_settings

logger = logging.getLogger("services.playwright_cleanup")
settings = get_settings()


def get_playwright_temp_dirs() -> List[Path]:
    """
    ИСПРАВЛЕНО: Получает список директорий с временными файлами Playwright.
    
    Returns:
        List[Path]: Список путей к временным директориям
    """
    temp_dirs = []
    
    # Windows: %TEMP%\playwright-*
    # Linux/Mac: /tmp/playwright-*
    import os
    temp_base = Path(os.environ.get('TEMP', os.environ.get('TMP', '/tmp')))
    
    # Ищем директории playwright-*
    if temp_base.exists():
        for item in temp_base.iterdir():
            if item.is_dir() and item.name.startswith('playwright-'):
                temp_dirs.append(item)
    
    # Также проверяем стандартные пути Playwright
    playwright_paths = [
        Path.home() / ".cache" / "ms-playwright",
        Path.home() / ".playwright",
    ]
    
    for path in playwright_paths:
        if path.exists():
            temp_dirs.append(path)
    
    return temp_dirs


def cleanup_playwright_temp_files(max_age_hours: int = 24) -> dict:
    """
    ИСПРАВЛЕНО: Очищает старые временные файлы Playwright.
    
    Args:
        max_age_hours: Максимальный возраст файлов в часах (по умолчанию 24 часа)
        
    Returns:
        dict: Статистика очистки
    """
    from datetime import datetime, timedelta
    import time
    
    stats = {
        "directories_checked": 0,
        "directories_removed": 0,
        "files_removed": 0,
        "bytes_freed": 0,
        "errors": []
    }
    
    cutoff_time = time.time() - (max_age_hours * 3600)
    
    try:
        temp_dirs = get_playwright_temp_dirs()
        stats["directories_checked"] = len(temp_dirs)
        
        for temp_dir in temp_dirs:
            try:
                if not temp_dir.exists():
                    continue
                
                # Проверяем возраст директории
                dir_mtime = temp_dir.stat().st_mtime
                if dir_mtime < cutoff_time:
                    # Директория старая - удаляем
                    try:
                        size = sum(f.stat().st_size for f in temp_dir.rglob('*') if f.is_file())
                        shutil.rmtree(temp_dir)
                        stats["directories_removed"] += 1
                        stats["bytes_freed"] += size
                        logger.info(f"Removed old Playwright temp directory: {temp_dir} ({size / 1024 / 1024:.1f} MB)")
                    except Exception as e:
                        stats["errors"].append(f"Error removing {temp_dir}: {e}")
                        logger.warning(f"Error removing Playwright temp directory {temp_dir}: {e}")
                else:
                    # Директория новая - очищаем только старые файлы внутри
                    for file_path in temp_dir.rglob('*'):
                        if file_path.is_file():
                            try:
                                file_mtime = file_path.stat().st_mtime
                                if file_mtime < cutoff_time:
                                    size = file_path.stat().st_size
                                    file_path.unlink()
                                    stats["files_removed"] += 1
                                    stats["bytes_freed"] += size
                            except Exception as e:
                                stats["errors"].append(f"Error removing {file_path}: {e}")
            except Exception as e:
                stats["errors"].append(f"Error processing {temp_dir}: {e}")
                logger.warning(f"Error processing Playwright temp directory {temp_dir}: {e}")
        
        if stats["directories_removed"] > 0 or stats["files_removed"] > 0:
            logger.info(
                f"Playwright cleanup: removed {stats['directories_removed']} dirs, "
                f"{stats['files_removed']} files, freed {stats['bytes_freed'] / 1024 / 1024:.1f} MB"
            )
        
    except Exception as e:
        logger.error(f"Error in Playwright cleanup: {e}", exc_info=True)
        stats["errors"].append(str(e))
    
    return stats
