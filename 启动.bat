@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo   B站收藏夹智能整理 - 一键启动
echo ================================================
echo.

echo [1/3] 检查依赖 ...
python -c "import fastapi,uvicorn,requests,qrcode" 2>NUL
if errorlevel 1 (
  echo   [错误] 缺少 Python 依赖，请先执行：
  echo          pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)
echo   依赖 OK

echo [2/3] 检查 8080 端口 ...
netstat -ano | findstr ":8080 " | findstr LISTENING >NUL
if not errorlevel 1 (
  echo   [提示] 8080 已被占用，可能已有一个服务在运行。
  echo          请先关掉那个服务的窗口，再运行本脚本。
  echo.
  pause
  exit /b 1
)
echo   端口空闲

echo [3/3] 启动服务 ...
echo   - 服务地址：http://127.0.0.1:8080
echo   - 关闭本窗口 = 停止服务
echo   - 操作记录会一条条显示在本窗口（同时写入 server.log）
echo.

REM 用 Edge 的完整路径直接打开，不经过 http 关联（避免弹出其它程序）
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if exist "%EDGE%" (
  echo   正在用 Edge 打开页面 ...
  start "" "%EDGE%" http://127.0.0.1:8080
) else (
  echo   [提示] 没找到 Edge，请手动在浏览器打开：http://127.0.0.1:8080
)
echo   若页面暂时打不开，等服务窗口出现 "Uvicorn running" 后按 F5 刷新。
echo.

REM 服务输出：直接显示在本窗口；日志由 Python 自己同时写入 server.log
python server.py --port 8080

echo.
echo 服务已退出。
pause
