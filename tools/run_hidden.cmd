@echo off
cd /d "%~dp0.."
netstat -ano | findstr ":9000 " | findstr "LISTENING" >nul
if not errorlevel 1 exit /b 0
set "PYW=D:\Software\Program-Ming\Python\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
if not exist "data\logs" mkdir "data\logs"
"%PYW%" run.py >> "data\logs\app.log" 2>&1
