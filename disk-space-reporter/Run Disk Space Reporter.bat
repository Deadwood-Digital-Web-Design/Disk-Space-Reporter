@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install it from python.org first.
    pause
    exit /b 1
)
python disk_space_reporter.py
pause
