@echo off
cd /d "%~dp0"

set "PORT=9000"
set "PY=D:\Software\Program-Ming\Python\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================
echo   Hotspot Collection Network
echo   http://127.0.0.1:%PORT%/
echo ============================================
echo.

netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo [INFO] Already running. Opening browser...
  start "" "http://127.0.0.1:%PORT%/"
  ping -n 5 127.0.0.1 >nul
  exit /b 0
)

start "" powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 60;$i++){try{$r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:9000/api/health'; if($r.StatusCode -eq 200){Start-Process 'http://127.0.0.1:9000/'; break}}catch{}; Start-Sleep -Milliseconds 700}"

echo Starting server (5-10s), browser will open automatically...
echo Close this window to stop the server.
echo.

"%PY%" run.py

echo.
echo [Stopped] Press any key to close.
pause >nul
