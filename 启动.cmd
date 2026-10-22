@echo off
cd /d "%~dp0"
start "" wscript "%~dp0Æô¶¯.vbs"
ping -n 8 127.0.0.1 >nul
start "" "http://127.0.0.1:9000/"
exit /b 0
