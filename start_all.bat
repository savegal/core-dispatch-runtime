@echo off
chcp 65001 > nul
echo Запуск процессов...
start "Backend (FastAPI)" cmd /k "chcp 65001 > nul && python run_server.py"
start "Telegram Bot" cmd /k "chcp 65001 > nul && python -m bot.app"
start "Executor" cmd /k "chcp 65001 > nul && python -m executor.executor"
echo Все 3 терминала запущены в отдельных окнах!
