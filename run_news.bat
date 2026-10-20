@echo off
REM Daily tech news runner - invoked by Windows Task Scheduler
REM The project now runs on GitHub Actions (08:00 Beijing time).
REM This local task is only a fallback: it runs when Actions did not
REM deliver today's mail. CI guard prevents sending twice a day.
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE=python"
where py >nul 2>nul && set "PYEXE=py"

REM Guard exit codes: 0 = already handled today, 1 = nobody handled it,
REM 2 = cannot tell (network/credential problem).
%PYEXE% ci_guard.py >> news_collector_run.log 2>&1
set "GUARD=%errorlevel%"
if "%GUARD%"=="0" (
    echo [run_news] ci-guard: already handled today, skip.>> news_collector_run.log
    exit /b 0
)
if "%GUARD%"=="2" (
    echo [run_news] ci-guard: cannot determine, skip to be safe.>> news_collector_run.log
    exit /b 0
)

echo [run_news] ci-guard: CI did not handle today, local fallback runs.>> news_collector_run.log
%PYEXE% news_collector.py >> news_collector_run.log 2>&1
exit /b %errorlevel%
