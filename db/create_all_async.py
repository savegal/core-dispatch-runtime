"""
Создает все таблицы в базе данных (async версия).
Для использования в async контексте.
"""
import asyncio
from db.database import engine, Base
from db.models import (  # ИСПРАВЛЕНО: Явный импорт вместо import *
    User, Account, Task, Proxy, Subscription, TaskTemplate,
    CommentTemplate, Log, Transaction, Pricing, AdminAction, AppSettings,
    CreatorSource, CreatorVideo  # Новые модели для парсинга видео
)


async def create_all_tables():
    """Создает все таблицы в async контексте."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


if __name__ == "__main__":
    asyncio.run(create_all_tables())
    print("Таблицы созданы (async)")










