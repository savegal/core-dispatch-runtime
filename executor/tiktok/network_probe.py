import logging
import json
from typing import Callable

logger = logging.getLogger("executor.tiktok.network_probe")


def make_probe_handler(task_id: int | None = None, account_id: int | None = None) -> Callable:
    """
    Создает обработчик network response для диагностики TikTok endpoints.
    Логирует только релевантные запросы (publish/list/like/reply).
    """
    task_info = f"task_id={task_id}, account_id={account_id}"

    async def _on_response(response):
        try:
            url = response.url or ""
            url_lower = url.lower()
            if not any(k in url_lower for k in ["comment", "reply", "like"]):
                return

            status = response.status
            try:
                data = await response.json()
            except Exception:
                data = None

            logger.info(
                f"[NET_PROBE] {task_info} status={status} url={url[:120]} data_keys="
                f"{list(data.keys())[:10] if isinstance(data, dict) else None}"
            )

            # Persist raw probe to file for later analysis
            try:
                record = {
                    "task_id": task_id,
                    "account_id": account_id,
                    "status": status,
                    "url": url,
                    "data": data if isinstance(data, dict) else None
                }
                with open("diagnostics/network_probe.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[NET_PROBE] error: {e}")

    return _on_response
