@echo off
setlocal
cd /d "%~dp0"

echo Exporting executor logs for analysis...
python scripts\export_executor_logs.py --zip
if errorlevel 1 (
    echo.
    echo Failed to export logs.
    pause
    exit /b 1
)

echo.
echo Done. Attach the newest logs\executor_analysis_*.zip file to Codex.
pause
