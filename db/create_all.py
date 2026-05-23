"""
Создает все таблицы в базе данных (синхронная версия).
Для использования в синхронном контексте или при инициализации.
"""
import sys
import os

# Добавляем корневую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import sync_engine, Base
from db.models import (  # ИСПРАВЛЕНО: Явный импорт вместо import *
    User, Account, Task, Proxy, Subscription, TaskTemplate, 
    CommentTemplate, Log, Transaction, Pricing, AdminAction, AppSettings,
    CreatorSource, CreatorVideo, VideoBatch, MonitoredBlogger, MonitorOutbox  # Новые модели для парсинга видео
)


if __name__ == "__main__":
    print("Creating database tables...")
    Base.metadata.create_all(bind=sync_engine)
    print("Done!")
