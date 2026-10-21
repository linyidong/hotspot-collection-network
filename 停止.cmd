@echo off
cd /d "%~dp0"
set "PORT=9000"
set "BRIDGE=8053"
echo Stopping Hotspot Collection Network (port %PORT% / bridge %BRIDGE%) ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%BRIDGE%" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%a >nul 2>&1
)
echo Done.
ping -n 2 127.0.0.1 >nul
