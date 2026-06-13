"""
Единая конфигурация логирования для всего приложения.
Вызывается один раз при старте приложения.

ИСПРАВЛЕНО: Добавлена поддержка structured logging (JSON) для улучшения наблюдаемости.
"""
import logging
import logging.handlers
import sys
import io
import json
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional


class SafeStreamHandler(logging.StreamHandler):
    """
    StreamHandler который не падает при ошибках кодировки.
    Заменяет проблемные символы на '?' вместо падения.
    """
    def emit(self, record):
        try:
            msg = self.format(record)
            # Сразу конвертируем в ASCII-safe строку
            safe_msg = msg.encode('ascii', errors='replace').decode('ascii')
            self.stream.write(safe_msg + self.terminator)
            self.flush()
        except Exception:
            pass  # Молча игнорируем - лучше чем падать
    
    def handleError(self, record):
        """Переопределяем чтобы не выводить 'Logging error'."""
        pass


class JSONFormatter(logging.Formatter):
    """
    ИСПРАВЛЕНО: JSON formatter для structured logging.
    
    Форматирует логи в JSON для лучшей интеграции с системами мониторинга
    (ELK, Loki, CloudWatch и т.д.).
    """
    
    def __init__(self, include_extra: bool = True):
        """
        Args:
            include_extra: Включать ли дополнительные поля из record (task_id, account_id и т.д.)
        """
        super().__init__()
        self.include_extra = include_extra
    
    def format(self, record: logging.LogRecord) -> str:
        """
        Форматирует log record в JSON.
        
        Args:
            record: Log record для форматирования
            
        Returns:
            str: JSON строка с логом
        """
        log_data: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Добавляем exception info если есть
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # ИСПРАВЛЕНО: Добавляем дополнительные поля для correlation (task_id, account_id и т.д.)
        if self.include_extra:
            # Извлекаем task_id, account_id и другие поля из extra
            extra_fields = {
                "task_id": getattr(record, "task_id", None),
                "account_id": getattr(record, "account_id", None),
                "proxy_id": getattr(record, "proxy_id", None),
                "video_url": getattr(record, "video_url", None),
                "comment_id": getattr(record, "comment_id", None),
                "error_type": getattr(record, "error_type", None),
            }
            # Удаляем None значения
            extra_fields = {k: v for k, v in extra_fields.items() if v is not None}
            if extra_fields:
                log_data["context"] = extra_fields
        
        # Добавляем все остальные поля из record.args если они есть
        if hasattr(record, "args") and record.args:
            try:
                # Пытаемся добавить args как дополнительные поля
                if isinstance(record.args, dict):
                    log_data.update(record.args)
            except Exception:
                pass
        
        try:
            return json.dumps(log_data, ensure_ascii=False, default=str)
        except Exception as e:
            # Fallback на обычное форматирование при ошибке
            return f'{{"error": "JSON formatting failed: {e}", "message": "{record.getMessage()}"}}'


def setup_logging(
    level: str = "INFO",
    format_string: str | None = None,
    log_file: str | None = None,
    use_json: bool = False,
    log_max_bytes: int | None = None,
    log_backup_count: int | None = None,
) -> None:
    """
    Настраивает логирование для всего приложения.
    
    ИСПРАВЛЕНО: Добавлена поддержка structured logging (JSON).
    
    Args:
        level: Уровень логирования (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format_string: Формат строки логов. Если None, используется стандартный
        log_file: Путь к файлу для сохранения логов. Если None, логи идут только в консоль
        use_json: Если True - использует JSON формат для structured logging
    """
    # Настройка UTF-8 для Windows консоли
    if sys.platform == 'win32':
        try:
            # Устанавливаем UTF-8 для консоли Windows
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)  # UTF-8 code page
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass
        
        # Переопределяем stdout/stderr с UTF-8 кодировкой
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, OSError):
            # Для старых версий Python или если reconfigure не работает
            try:
                sys.stdout = io.TextIOWrapper(
                    sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True
                )
                sys.stderr = io.TextIOWrapper(
                    sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True
                )
            except Exception:
                pass
    
    # ИСПРАВЛЕНО: Определяем formatter в зависимости от режима
    if use_json:
        formatter = JSONFormatter(include_extra=True)
        # Для JSON формата не нужен format_string
    else:
        if format_string is None:
            format_string = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        formatter = logging.Formatter(format_string)
    
    # Создаём безопасный handler который не падает при ошибках кодировки
    stream_handler = SafeStreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    
    handlers = [stream_handler]
    
    if log_file:
        # ИСПРАВЛЕНО: Используем RotatingFileHandler для ротации логов
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rotation_max_bytes = max(1, int(log_max_bytes or (10 * 1024 * 1024)))
        rotation_backup_count = max(1, int(log_backup_count or 5))
        
        # RotatingFileHandler: maxBytes=10MB, backupCount=5 (сохраняем последние 5 файлов)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=rotation_max_bytes,
            backupCount=rotation_backup_count,
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=format_string,
        handlers=handlers,
        force=True  # Перезаписываем существующие настройки
    )
    
    # Заменяем handlers для всех существующих логгеров (включая SQLAlchemy)
    for logger_name in logging.Logger.manager.loggerDict:
        existing_logger = logging.getLogger(logger_name)
        if existing_logger.handlers:
            existing_logger.handlers.clear()
            for handler in handlers:
                existing_logger.addHandler(handler)

    for noisy_logger_name in ("httpx", "httpcore"):
        logging.getLogger(noisy_logger_name).setLevel(logging.WARNING)
