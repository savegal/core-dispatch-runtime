"""
Конфигурация pytest для всех тестов.
Содержит общие fixtures и настройки.
"""
import sys
import os
from pathlib import Path
import uuid

# Добавляем корневую директорию проекта в PYTHONPATH
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pytest
import asyncio
from typing import AsyncGenerator, Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from datetime import datetime, timezone

from db.database import Base
from db.models import (
    User, Account, Proxy, Task, TaskTemplate, 
    Subscription, Log, Pricing, Transaction, AdminAction, AppSettings
)
from decimal import Decimal

def _build_test_db_urls() -> tuple[str, str]:
    sync_override = os.getenv("TEST_DB_URL")
    async_override = os.getenv("TEST_DB_URL_ASYNC")
    if sync_override and async_override:
        return sync_override, async_override

    unique_db_name = f"test_{uuid.uuid4().hex}.db"
    db_file = Path.cwd() / unique_db_name
    sync_url = sync_override or f"sqlite:///{db_file.as_posix()}"
    async_url = async_override or f"sqlite+aiosqlite:///{db_file.as_posix()}"
    return sync_url, async_url


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    """Создаёт тестовую БД сессию для каждого теста."""
    sync_url, _ = _build_test_db_urls()
    test_engine = create_engine(sync_url, connect_args={"check_same_thread": False})
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    Base.metadata.create_all(bind=test_engine)
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)
        test_engine.dispose()

        if sync_url.startswith("sqlite:///"):
            db_path = Path(sync_url.replace("sqlite:///", "", 1))
            try:
                db_path.unlink(missing_ok=True)
            except Exception:
                pass


@pytest.fixture(scope="function")
async def async_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Создаёт async тестовую БД сессию для каждого теста."""
    _, async_url = _build_test_db_urls()
    test_async_engine = create_async_engine(
        async_url,
        echo=False,
        future=True,
    )
    TestAsyncSessionLocal = async_sessionmaker(
        test_async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with test_async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with TestAsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
            async with test_async_engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
            await test_async_engine.dispose()

            if async_url.startswith("sqlite+aiosqlite:///"):
                db_path = Path(async_url.replace("sqlite+aiosqlite:///", "", 1))
                try:
                    db_path.unlink(missing_ok=True)
                except Exception:
                    pass


@pytest.fixture
def sample_user(db_session: Session) -> User:
    """Создаёт тестового пользователя."""
    user = User(
        telegram_id=12345,
        username="test_user",
        role="user",
        balance=1000.00,
        is_blocked=False
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def sample_admin(db_session: Session) -> User:
    """Создаёт тестового админа."""
    admin = User(
        telegram_id=99999,
        username="admin_user",
        role="admin",
        balance=0.00,
        is_blocked=False
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


@pytest.fixture
def sample_account(db_session: Session) -> Account:
    """Создаёт тестовый аккаунт."""
    account = Account(
        label="test_account_1",
        cookies='{"sessionid": "test123"}',
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def sample_proxy(db_session: Session) -> Proxy:
    """Создаёт тестовый прокси."""
    proxy = Proxy(
        proxy="http://user:pass@proxy.example.com:8080",
        status="active",
        response_time_ms=100,
        fail_count=0
    )
    db_session.add(proxy)
    db_session.commit()
    db_session.refresh(proxy)
    return proxy


@pytest.fixture
def sample_task(db_session: Session, sample_user: User) -> Task:
    """Создаёт тестовую задачу."""
    task = Task(
        user_id=sample_user.id,
        status="pending",
        main_comment="Test comment",
        video_urls=["https://www.tiktok.com/@test/video/123"],
        progress=0,
        attempts=0,
        total_cost=10.00
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


@pytest.fixture
def pricing_config(db_session: Session):
    """Создаёт конфигурацию цен."""
    pricing = Pricing(
        main_comment_price=1.00,
        reply_price=0.50,
        like_price=0.10
    )
    db_session.add(pricing)
    db_session.commit()
    return pricing


@pytest.fixture(scope="session")
def event_loop():
    """Создаёт event loop для async тестов."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
