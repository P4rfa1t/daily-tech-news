@echo off
REM Daily tech news runner - invoked by Windows Task Scheduler
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py news_collector.py >> news_collector_run.log 2>&1
) else (
    python news_collector.py >> news_collector_run.log 2>&1
)
