@echo off
cd /d "%~dp0"
setlocal enabledelayedexpansion
echo Stopping Hotspot Collection Network ...

rem 1) 按 pid 文件精确结束桥（端口是自动分配的）
if exist "data\logs\bridge.pid" (
  set /p BPID=<"data\logs\bridge.pid"
  if not "!BPID!"=="" taskkill /PID !BPID! /F >nul 2>&1
  del /q "data\logs\bridge.pid" >nul 2>&1
)

rem 2) 主服务端口 9000
for /f "tokens=5" %%A in ('netstat -ano ^| findstr ":9000 " ^| findstr "LISTENING"') do (
  taskkill /PID %%A /F >nul 2>&1
)

rem 3) 桥端口兜底：扫 8053-8075
for /l %%Q in (8053,1,8075) do (
  for /f "tokens=5" %%A in ('netstat -ano ^| findstr ":%%Q " ^| findstr "LISTENING"') do (
    taskkill /PID %%A /F >nul 2>&1
  )
)

if exist "data\logs\app.pid" del /q "data\logs\app.pid" >nul 2>&1
echo Done.
endlocal
exit /b 0
