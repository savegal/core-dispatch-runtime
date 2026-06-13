@echo off
setlocal
cd /d "%~dp0"

echo Preparing a fresh executor log capture marker...
python scripts\export_executor_logs.py --mark-start
if errorlevel 1 (
    echo.
    echo Failed to prepare log capture marker.
    pause
    exit /b 1
)

echo.
echo Ready. Start the executor, run your test orders, then run export_logs_for_analysis.bat.
pause
