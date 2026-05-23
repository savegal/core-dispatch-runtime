"""
Скрипт для создания профилей браузера для всех аккаунтов из БД.

Запускать ПОСЛЕ того как bas_http_server.py уже запущен:
  1. В одном окне: python bas_scripts/bas_http_server.py
  2. В другом окне: python bas_scripts/setup_all_profiles.py

Что делает:
- Берёт все активные аккаунты из БД
- Для каждого создаёт профиль браузера с куками
- После этого bas_http_server.py сможет использовать эти профили
"""
import asyncio
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from db.database import SessionLocal
from db.models import Account

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("setup_profiles")

BAS_SERVER = "http://127.0.0.1:8765"


async def setup_all():
    # Проверяем что сервер запущен
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{BAS_SERVER}/health")
            if r.status_code != 200:
                raise Exception(f"HTTP {r.status_code}")
        logger.info("BAS server is running OK")
    except Exception as e:
        logger.error("BAS server not available at %s: %s", BAS_SERVER, e)
        logger.error("Запусти сначала: python bas_scripts/bas_http_server.py")
        return

    db = SessionLocal()
    try:
        accounts = db.query(Account).filter(Account.status == "active").all()
        logger.info("Found %d active accounts", len(accounts))

        success = 0
        failed = 0

        for i, acc in enumerate(accounts, 1):
            if not acc.cookies:
                logger.warning("[%d/%d] Account %d: no cookies, skipping", i, len(accounts), acc.id)
                failed += 1
                continue

            logger.info("[%d/%d] Setting up profile for account %d (proxy=%s)",
                       i, len(accounts), acc.id, "yes" if acc.proxy else "no")

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    r = await client.post(f"{BAS_SERVER}/setup_account", json={
                        "account_id": acc.id,
                        "cookies_json": acc.cookies,
                        "proxy": acc.proxy or "",
                    })
                    result = r.json()

                if result.get("status") == "success":
                    logger.info("[%d/%d] Account %d: profile created OK", i, len(accounts), acc.id)
                    success += 1
                else:
                    logger.warning("[%d/%d] Account %d: failed — %s",
                                  i, len(accounts), acc.id, result.get("status"))
                    failed += 1

            except Exception as e:
                logger.error("[%d/%d] Account %d: error — %s", i, len(accounts), acc.id, e)
                failed += 1

            # Небольшая пауза между профилями
            await asyncio.sleep(1.0)

        logger.info("Done: %d success, %d failed out of %d accounts", success, failed, len(accounts))

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(setup_all())
