# TeleAgent Review Sink —— 卸载脚本（PowerShell）
#
# 还原 internal-debug.yaml、删自启项、停掉 sink 和看门狗。

[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$ErrorView = "NormalView"

$TeleAgentDir = Join-Path $env:USERPROFILE ".local\share\TeleAgent"
$DebugFile = Join-Path $TeleAgentDir "internal-debug.yaml"
$StartupDir = [Environment]::GetFolderPath("Startup")
$VbsPath = Join-Path $StartupDir "teleagent-sink.vbs"
$InstallDir = Join-Path $env:USERPROFILE "teleagent-review-sink"

# 1. 还原 internal-debug.yaml
if (Test-Path "$DebugFile.bak") {
	Copy-Item "$DebugFile.bak" $DebugFile -Force
	Write-Host "已从备份还原 $DebugFile" -ForegroundColor Green
} elseif (Test-Path $DebugFile) {
	Remove-Item $DebugFile -Force
	Write-Host "已删除 $DebugFile（TeleAgent 恢复默认行为）" -ForegroundColor Green
} else {
	Write-Host "$DebugFile 不存在，无需处理" -ForegroundColor Gray
}

# 2. 删自启项
if (Test-Path $VbsPath) {
	Remove-Item $VbsPath -Force
	Write-Host "已删除自启项 $VbsPath" -ForegroundColor Green
}

# 3. 停掉 sink / 看门狗
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
	Where-Object { $_.CommandLine -match "review-sink\.js|watchdog\.js" } |
	ForEach-Object {
		Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
		Write-Host "已终止进程 $($_.ProcessId) ($($_.Name))" -ForegroundColor Green
	}

# 4. （可选）删除安装目录
if (Test-Path $InstallDir) {
	$answer = Read-Host "是否删除安装目录 $InstallDir？(y/N)"
	if ($answer -eq "y" -or $answer -eq "Y") {
		Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
		Write-Host "已删除 $InstallDir" -ForegroundColor Green
	}
}

Write-Host ""
Write-Host "卸载完成。请重启一次 TeleAgent 让默认配置生效。" -ForegroundColor Cyan
