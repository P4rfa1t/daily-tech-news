@echo off
REM Re-register the DailyTechNews scheduled task with administrator rights.
REM Double-click this file; accept the UAC prompt.
cd /d "%~dp0"
echo Requesting administrator rights...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0setup_task.ps1')"
echo.
echo If setup_task.ps1 reported success, the task now points to this folder.
echo Verify: Task Scheduler Library ^> DailyTechNews
echo.
pause
