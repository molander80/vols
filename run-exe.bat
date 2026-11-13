@echo off
setlocal EnableExtensions
title VOLS - KMZ + GPON Viewer (exe)
cd /d "%~dp0"

rem Run the packed app.exe. Keeps the console open, so any startup error
rem stays visible instead of the window closing in a flash.

set "EXE=%~dp0app.exe"
if not exist "%EXE%" if exist "%~dp0dist\app.exe" set "EXE=%~dp0dist\app.exe"

if not exist "%EXE%" (
  echo [ERROR] app.exe not found next to this file or in dist\
  echo         Put run-exe.bat next to app.exe or in the project root.
  pause
  exit /b 1
)

echo [INFO] exe: %EXE%
echo [INFO] URL: http://127.0.0.1:5000
echo [INFO] Press Ctrl+C in this window to stop the server.
echo.

rem Open the browser a bit later, when the server has started.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://127.0.0.1:5000'"

"%EXE%"
set "RC=%ERRORLEVEL%"
echo.
echo [INFO] Server stopped, exit code: %RC%
if not "%RC%"=="0" (
  echo.
  echo [HINT] If the code is 1 and the log says "Address already in use",
  echo        port 5000 is busy. Close the other server window and retry.
)
pause
