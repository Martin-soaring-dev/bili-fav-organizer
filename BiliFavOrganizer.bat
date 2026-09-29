@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Release ZIP contains a self-contained application; source archives use Python bootstrap.
if exist "%~dp0BiliFavOrganizer.exe" goto run_portable

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0BiliFavOrganizer.ps1"
if errorlevel 1 (
  echo.
  echo 启动失败，请查看上方错误信息。
  pause
)
exit /b

:run_portable
"%~dp0BiliFavOrganizer.exe" %*
exit /b %errorlevel%
