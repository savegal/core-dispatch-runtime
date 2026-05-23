"""
Диагностика и отладка для TikTok автоматизации.

Расширенная диагностика с консольными ошибками, failed requests и таймлайном шагов.
"""
import json
import logging
import time
from pathlib import Path
from typing import Optional, Tuple, Dict, List
from datetime import datetime
from patchright.async_api import Page
from db.models import Account

logger = logging.getLogger("executor.tiktok.diagnostics")


class DiagnosticCollector:
    """Собирает диагностическую информацию во время выполнения операции."""
    
    def __init__(self, page: Page):
        self.page = page
        self.console_errors: List[Dict] = []
        self.failed_requests: List[Dict] = []
        self.timeline: List[Dict] = []
        self.start_time = time.time()
        
        # Подписываемся на события
        self._setup_listeners()
    
    def _setup_listeners(self):
        """Настраивает слушатели событий страницы."""
        # Консольные ошибки
        self.page.on("console", self._on_console)
        # Ошибки загрузки ресурсов
        self.page.on("requestfailed", self._on_request_failed)
    
    def _on_console(self, msg):
        """Обработчик консольных сообщений."""
        if msg.type == "error":
            self.console_errors.append({
                "type": "console_error",
                "text": msg.text,
                "timestamp": time.time() - self.start_time
            })
    
    def _on_request_failed(self, request):
        """Обработчик неудачных запросов."""
        self.failed_requests.append({
            "type": "request_failed",
            "url": request.url,
            "method": request.method,
            "failure": request.failure.get("errorText", "") if request.failure else "",
            "timestamp": time.time() - self.start_time
        })
    
    def record_step(self, step_name: str, status: str = "ok", duration_ms: Optional[float] = None):
        """
        Записывает шаг в таймлайн.
        
        Args:
            step_name: Название шага
            status: Статус ("ok", "error", "warning")
            duration_ms: Длительность шага в миллисекундах
        """
        self.timeline.append({
            "step": step_name,
            "status": status,
            "duration_ms": duration_ms,
            "timestamp": time.time() - self.start_time
        })
    
    def get_report(self) -> Dict:
        """Возвращает полный отчёт диагностики."""
        return {
            "console_errors": self.console_errors,
            "failed_requests": self.failed_requests,
            "timeline": self.timeline,
            "total_duration_ms": (time.time() - self.start_time) * 1000
        }


async def save_diagnostic(
    page: Page,
    account: Account,
    task_id: Optional[int],
    error_type: str
) -> Tuple[Optional[str], Optional[str]]:
    """
    Сохраняет диагностическую информацию (скриншот + HTML + JSON отчёт).
    
    Args:
        page: Страница Playwright
        account: Аккаунт из БД
        task_id: Опциональный ID задачи
        error_type: Тип ошибки (для имени файла)
        
    Returns:
        tuple: (screenshot_path, html_path) или (None, None) при ошибке
    """
    screenshot_path = None
    html_path = None
    json_path = None
    
    try:
        # Создаём папку для диагностики
        diag_dir = Path("diagnostics")
        diag_dir.mkdir(exist_ok=True)
        
        timestamp = int(time.time())
        prefix = f"task_{task_id}_acc_{account.id}_{timestamp}" if task_id else f"acc_{account.id}_{timestamp}"
        
        # Сохраняем скриншот
        screenshot_path = str(diag_dir / f"{prefix}_{error_type}.png")
        await page.screenshot(path=screenshot_path, full_page=True)
        logger.info(f"[DIAGNOSTICS] Screenshot saved: {screenshot_path}")
        
        # Сохраняем HTML
        html_path = str(diag_dir / f"{prefix}_{error_type}.html")
        html_content = await page.content()
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        logger.info(f"[DIAGNOSTICS] HTML saved: {html_path}")
        
        # Собираем дополнительную диагностику
        diagnostic_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "account_id": account.id,
            "account_label": account.label,
            "task_id": task_id,
            "error_type": error_type,
            "page_url": page.url,
            "page_title": await page.title(),
            "console_errors": [],
            "failed_requests": [],
        }
        
        # Пытаемся собрать консольные ошибки (если были подписаны)
        try:
            # Это работает только если мы подписались на события до выполнения операции
            # Здесь просто собираем то, что можем
            pass
        except Exception:
            pass
        
        # Сохраняем JSON отчёт
        json_path = str(diag_dir / f"{prefix}_{error_type}.json")
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(diagnostic_data, f, indent=2, ensure_ascii=False)
        logger.info(f"[DIAGNOSTICS] JSON report saved: {json_path}")
        
    except Exception as e:
        logger.debug(f"[DIAGNOSTICS] Failed to save diagnostic files: {e}")
    
    return screenshot_path, html_path

