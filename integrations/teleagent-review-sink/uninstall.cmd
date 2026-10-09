@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
echo.
echo  TeleAgent Review Sink 卸载程序
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
echo.
pause
