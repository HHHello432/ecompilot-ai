@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
echo.
echo  TeleAgent Review Sink 安装程序
echo  拦截 TeleAgent 的工具指令审查上传，阻止本机数据外发
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
