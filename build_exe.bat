@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Собирает AionCubeRadar.exe (один файл) рядом с этим .bat. Права администратора для сборки не нужны.

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
    python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo Python не найден. Установите: winget install -e --id Python.Python.3.12
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo Создаю окружение .venv ...
    %PY% -m venv .venv || (echo Не удалось создать venv & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt pyinstaller || (echo Не удалось установить библиотеки & pause & exit /b 1)
".venv\Scripts\python.exe" -c "import tkinter" || (echo В этом Python нет tkinter - оверлей не заработает & pause & exit /b 1)

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --noconsole --uac-admin ^
    --name AionCubeRadar --icon radar.ico ^
    --collect-submodules scapy --hidden-import lz4.block ^
    run.py
echo.
if exist "dist\AionCubeRadar.exe" (
    copy /y "dist\AionCubeRadar.exe" "AionCubeRadar.exe" >nul
    echo Готово: AionCubeRadar.exe ^(настройки будут в папке radar_data рядом с ним^)
) else (
    echo Сборка не удалась, смотрите сообщения выше.
)
pause
