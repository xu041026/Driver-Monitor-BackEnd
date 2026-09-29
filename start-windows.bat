@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" start.py
pause
exit /b
:failed
echo 安装或启动失败，请检查上方信息。需要 Python 3.10 或更高版本。
pause
exit /b 1
