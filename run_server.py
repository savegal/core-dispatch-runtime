#!/usr/bin/env python
"""
Wrapper для запуска uvicorn с правильной event loop policy для Windows.
Это необходимо для работы Playwright в API endpoints.
"""
import sys
import asyncio

# ИСПРАВЛЕНО: Устанавливаем ProactorEventLoopPolicy ДО импорта uvicorn
# Это критично для Windows, чтобы Playwright мог работать в API endpoints
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    print("Windows ProactorEventLoopPolicy установлена для поддержки subprocess")

# Теперь импортируем и запускаем uvicorn
if __name__ == "__main__":
    import uvicorn
    from config.settings import get_settings
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=bool(settings.DEBUG or str(settings.ENV).lower() in ("dev", "development")),
    )







