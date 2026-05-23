"""
ИСПРАВЛЕНО: Очистка ресурсов Playwright для предотвращения переполнения диска.

Advanced Layer: Периодическая очистка временных файлов браузера.

КРИТИЧНО: Playwright создает временные файлы профилей в /tmp (Linux) или %TEMP% (Windows).
При работе 24/7 эти файлы накапливаются и могут переполнить диск.
"""
import logging
import shutil
from pathlib import Path
from typing import Optional
from config.settings import get_settings

logger = logging.getLogger("services.resource_cleanup")
settings = get_settings()


def cleanup_playwright_temp_files(max_age_hours: int = 24) -> int:
    """
    ИСПРАВЛЕНО: Очищает старые временные файлы Playwright.
    
    Удаляет профили браузеров старше max_age_hours часов.
    
    Args:
        max_age_hours: Максимальный возраст файлов в часах (по умолчанию 24 часа)
        
    Returns:
        int: Количество удаленных файлов/папок
    """
    import time
    import os
    
    removed_count = 0
    current_time = time.time()
    max_age_seconds = max_age_hours * 3600
    
    # Пути к временным файлам Playwright
    temp_dirs = []
    
    # Windows
    if os.name == 'nt':
        temp_base = Path(os.environ.get('TEMP', os.environ.get('TMP', 'C:\\Temp')))
        # Playwright создает папки типа playwright-* в TEMP
        temp_dirs.extend(temp_base.glob('playwright-*'))
        temp_dirs.extend(temp_base.glob('pw-*'))
    else:
        # Linux/Mac
        temp_base = Path('/tmp')
        temp_dirs.extend(temp_base.glob('playwright-*'))
        temp_dirs.extend(temp_base.glob('pw-*'))
        # Также проверяем ~/.cache/ms-playwright
        cache_dir = Path.home() / '.cache' / 'ms-playwright'
        if cache_dir.exists():
            temp_dirs.append(cache_dir)
    
    for temp_dir in temp_dirs:
        try:
            if not temp_dir.exists():
                continue
            
            # Проверяем возраст
            if temp_dir.is_dir():
                # Для папок проверяем время модификации
                try:
                    mtime = temp_dir.stat().st_mtime
                    age_seconds = current_time - mtime
                    
                    if age_seconds > max_age_seconds:
                        # Удаляем старую папку
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        removed_count += 1
                        logger.debug(f"Removed old Playwright temp dir: {temp_dir}")
                except Exception as e:
                    logger.debug(f"Error checking/removing {temp_dir}: {e}")
            elif temp_dir.is_file():
                # Для файлов тоже проверяем возраст
                try:
                    mtime = temp_dir.stat().st_mtime
                    age_seconds = current_time - mtime
                    
                    if age_seconds > max_age_seconds:
                        temp_dir.unlink()
                        removed_count += 1
                        logger.debug(f"Removed old Playwright temp file: {temp_dir}")
                except Exception as e:
                    logger.debug(f"Error checking/removing {temp_dir}: {e}")
        except Exception as e:
            logger.debug(f"Error processing {temp_dir}: {e}")
            continue
    
    if removed_count > 0:
        logger.info(f"Cleaned up {removed_count} old Playwright temp files/dirs")
    
    return removed_count


def cleanup_diagnostics_files(max_age_hours: int = 48) -> int:
    """
    ИСПРАВЛЕНО: Очищает старые диагностические файлы (скриншоты, HTML).
    
    Args:
        max_age_hours: Максимальный возраст файлов в часах (по умолчанию 48 часов)
        
    Returns:
        int: Количество удаленных файлов
    """
    import time
    
    removed_count = 0
    current_time = time.time()
    max_age_seconds = max_age_hours * 3600
    
    diagnostics_dir = Path("diagnostics")
    if not diagnostics_dir.exists():
        return 0
    
    try:
        for file_path in diagnostics_dir.iterdir():
            try:
                if not file_path.is_file():
                    continue
                
                # Проверяем возраст
                mtime = file_path.stat().st_mtime
                age_seconds = current_time - mtime
                
                if age_seconds > max_age_seconds:
                    file_path.unlink()
                    removed_count += 1
            except Exception as e:
                logger.debug(f"Error removing diagnostic file {file_path}: {e}")
                continue
        
        if removed_count > 0:
            logger.info(f"Cleaned up {removed_count} old diagnostic files")
        
    except Exception as e:
        logger.warning(f"Error cleaning up diagnostic files: {e}")
    
    return removed_count


def cleanup_all_resources() -> dict:
    """
    ИСПРАВЛЕНО: Выполняет полную очистку всех ресурсов.
    
    Returns:
        dict: Статистика очистки
    """
    playwright_removed = cleanup_playwright_temp_files(max_age_hours=24)
    diagnostics_removed = cleanup_diagnostics_files(max_age_hours=48)
    
    return {
        "playwright_files": playwright_removed,
        "diagnostics_files": diagnostics_removed,
        "total": playwright_removed + diagnostics_removed
    }
