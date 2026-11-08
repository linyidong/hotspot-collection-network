@echo off
REM ============================================================
REM  3D 大屏静态稿预览器
REM  为什么需要它：浏览器在 file:// 下会按跨域拦截 WebGL 贴图，
REM  双击 html 只能看到"黑球"。必须用本地 http 服务打开。
REM  用法：双击本文件，浏览器会自动打开。
REM ============================================================
setlocal
cd /d "%~dp0static"

set PORT=8899
set PY=python
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 python。请先安装 Python 3.11+，安装时勾选 "Add Python to PATH"。
  pause
  exit /b 1
)

echo 正在启动预览服务 http://127.0.0.1:%PORT%/mockup-3d.html
start "" "http://127.0.0.1:%PORT%/mockup-3d.html"
%PY% -m http.server %PORT% --bind 127.0.0.1
endlocal
