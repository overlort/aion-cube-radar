@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Запуск из исходников (без сборки exe). Сниффер видит трафик только с правами администратора.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -Verb RunAs -ErrorAction Stop; exit 0 } catch { exit 1 }"
    if not errorlevel 1 exit /b
    echo Права администратора не дали - радар не увидит трафик.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    set "PY="
    py -3 --version >nul 2>&1 && set "PY=py -3"
    if not defined PY set "PY=python"
    echo Создаю окружение .venv ...
    call %%PY%% -m venv .venv || (pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (pause & exit /b 1)
)
start "" ".venv\Scripts\pythonw.exe" run.py
