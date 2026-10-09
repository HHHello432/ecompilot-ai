@echo off
chcp 65001 >nul 2>&1
echo.
echo  TeleAgent Review Sink 状态检查
echo  ==============================
echo.
netstat -ano | findstr "127.0.0.1:9923" | findstr "LISTENING" >nul && (
    echo  [OK] sink 正在监听 127.0.0.1:9923
) || (
    echo  [XX] sink 未运行
)
echo.
echo  最近 10 条审计日志（BLOCK=已拦截审查，PASS=放行给真实网关）：
echo  --------------------------------------------------
type "%USERPROFILE%\teleagent-review-sink\sink.log" 2>nul | more +0
echo.
echo  完整日志：%USERPROFILE%\teleagent-review-sink\sink.log
echo.
pause
