@echo off
setlocal EnableExtensions
title VOLS - KMZ + GPON Viewer

rem ---------------------------------------------------------------------------
rem Launches the project from the folder that contains this start.bat, i.e.
rem     C:\Users\<user>\projct\VOLS
rem The path comes from %~dp0 on purpose, not from a hardcoded literal: cmd
rem decodes .bat text with the current console code page, so a Cyrillic path
rem in the file breaks when the code page differs. A desktop shortcut to this
rem file is fine, %~dp0 is always the folder of the target file.
rem ---------------------------------------------------------------------------

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

if not exist "%PROJECT_DIR%\app.py" (
  echo [ERROR] app.py not found in "%PROJECT_DIR%"
  echo         Keep start.bat in the project root and run it from there.
  pause
  exit /b 1
)

cd /d "%PROJECT_DIR%"
if errorlevel 1 (
  echo [ERROR] cannot change directory to "%PROJECT_DIR%"
  pause
  exit /b 1
)

rem Use the venv of this project explicitly: PATH may still point to another
rem copy of the project (for example C:\VOLS\.venv).
set "PY=%PROJECT_DIR%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] venv interpreter not found: "%PY%"
  echo         Create it once:
  echo             python -m venv .venv
  echo             .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)

echo [INFO] Project dir: %PROJECT_DIR%
echo [INFO] Interpreter: %PY%
echo [INFO] URL:         http://127.0.0.1:5000
echo.

if /i "%~1"=="--check" (
  echo [CHECK] OK
  exit /b 0
)

"%PY%" app.py
set "RC=%ERRORLEVEL%"
echo.
echo [INFO] Server stopped, exit code: %RC%
pause
