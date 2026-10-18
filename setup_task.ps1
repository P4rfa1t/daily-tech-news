# ==============================================================
#  注册 Windows 定时任务：每天 08:00 自动收集并发送科技新闻
#  以管理员身份运行 PowerShell 后执行本脚本
#     powershell -ExecutionPolicy Bypass -File .\setup_task.ps1
# ==============================================================
$ErrorActionPreference = 'Stop'

$TaskName = 'DailyTechNews'
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Dir 'news_collector.py'
$Bat = Join-Path $Dir 'run_news.bat'
$Vbs = Join-Path $Dir 'run_hidden.vbs'

if (-not (Test-Path $Script)) { throw "找不到 $Script" }
if (-not (Test-Path $Bat))    { throw "找不到 $Bat" }
if (-not (Test-Path $Vbs))    { throw "找不到 $Vbs" }

$RunAt = '08:00'

Write-Host "任务名称 : $TaskName" -ForegroundColor Cyan
Write-Host "工作目录 : $Dir" -ForegroundColor Cyan
Write-Host "每天运行 : $RunAt" -ForegroundColor Cyan

# 动作：用 wscript 静默调用 run_news.bat（不弹出 cmd 窗口）
$Action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$Vbs`"" -WorkingDirectory $Dir

# 触发器1：每天 08:00；触发器2：每次登录时（用于补跑，脚本内部按日期去重）
$TriggerDaily = New-ScheduledTaskTrigger -Daily -At $RunAt
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn
$Triggers = @($TriggerDaily, $TriggerLogon)

# 设置：错过时间后补跑、允许电池运行、任务超时 30 分钟
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

# 注销旧的同名任务（如果存在）
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# 注册任务
$desc = '每天 08:00 自动收集 AI/科学/医疗/科技行业新闻并发送邮件（登录时补跑）'
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers -Settings $Settings -Description $desc | Out-Null

Write-Host ""
Write-Host "OK 定时任务已创建：$TaskName" -ForegroundColor Green
Write-Host "   查看/修改：任务计划程序 -> 任务计划程序库 -> $TaskName" -ForegroundColor Gray
Write-Host "   立即测试：Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host '   删除任务：Unregister-ScheduledTask -TaskName ''DailyTechNews'' -Confirm:$false' -ForegroundColor Gray
