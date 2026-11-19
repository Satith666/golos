@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Golos: сборка программы
set "PYTHONUTF8=1"
set "VENV=%~dp0.build"
set "VPY=%VENV%\Scripts\python.exe"

echo.
echo Собираю Golos. Обычно это занимает 2-5 минут.
echo.

"%VPY%" -c "import sys" >nul 2>&1
if not errorlevel 1 goto deps

if exist "%VENV%" rmdir /s /q "%VENV%"

for %%V in (3.12 3.13 3.11 3.10) do (
  py -%%V -m venv "%VENV%" >nul 2>&1
  if exist "%VPY%" goto deps
)

python -m venv "%VENV%" >nul 2>&1
if exist "%VPY%" goto deps

echo Python не найден, скачиваю его с python.org...
set "SETUP=%TEMP%\golos-python-setup.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $env:SETUP"
if not exist "%SETUP%" goto nopython
echo Устанавливаю Python только для твоего пользователя...
"%SETUP%" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0
del "%SETUP%" >nul 2>&1
set "BASEPY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%BASEPY%" goto nopython
"%BASEPY%" -m venv "%VENV%"
if not exist "%VPY%" goto nopython

:deps
echo Скачиваю библиотеки...
"%VPY%" -m pip install --disable-pip-version-check --quiet -r requirements.txt
if errorlevel 1 goto nodeps

echo Собираю Golos.exe...
"%VPY%" -m PyInstaller --noconfirm --clean --log-level ERROR --onefile --windowed --name Golos --icon icon.ico --add-data "ui.html;." --collect-all _sounddevice_data --hidden-import pystray._win32 app.py
if errorlevel 1 goto nobuild
if not exist "dist\Golos.exe" goto nobuild
copy /y "dist\Golos.exe" "Golos.exe" >nul
if errorlevel 1 goto locked

echo Проверяю собранную программу...
if exist selftest.txt del selftest.txt
start "" /wait "Golos.exe" --selftest
if not exist selftest.txt goto blocked
type selftest.txt
findstr /c:"RESULT: OK" selftest.txt >nul
if errorlevel 1 goto failed

rmdir /s /q build >nul 2>&1
del Golos.spec >nul 2>&1
echo.
echo Готово! Программа лежит здесь: %~dp0Golos.exe
echo.
pause
exit /b 0

:nopython
echo.
echo Не получилось установить Python.
echo Поставь его вручную с python.org (версия 3.12) и запусти BUILD.bat ещё раз.
goto end
:nodeps
echo.
echo Не получилось скачать библиотеки. Проверь интернет или включи VPN и запусти BUILD.bat ещё раз.
goto end
:nobuild
echo.
echo Сборка не удалась. Пришли мне весь текст из этого окна.
goto end
:locked
echo.
echo Golos.exe сейчас запущен. Закрой его через значок возле часов и запусти BUILD.bat ещё раз.
goto end
:blocked
echo.
echo Golos.exe не запустился для проверки. Скорее всего, его заблокировал антивирус.
echo Добавь эту папку в исключения Защитника Windows и запусти BUILD.bat ещё раз.
goto end
:failed
echo.
echo Проверка нашла ошибку. Пришли мне файл selftest.txt.
:end
echo.
pause
exit /b 1
