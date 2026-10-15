# 注册「批次看门狗」计划任务：登录时 + 每 N 分钟检查批次是否还活着，已死则续跑。
#
# 用法（注册到当前用户，不需要管理员）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_watchdog.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_watchdog.ps1 -IntervalMinutes 30
# 查看 / 卸载：
#   Get-ScheduledTaskInfo -TaskName BilibiliTranscriptWatchdog
#   Unregister-ScheduledTask -TaskName BilibiliTranscriptWatchdog
#
# 路径从脚本自身位置推导，换机后不用改。中文要写在 -Description 里，所以本文件必须
# 存为「带 BOM 的 UTF-8」——Windows PowerShell 5.1 会把无 BOM 的 UTF-8 当本地代码页
# 读，中文会把字符串引号撑破（2026-09-18 实测踩过这个坑）。
param([int]$IntervalMinutes = 15)

$ErrorActionPreference = 'Stop'

$skill  = Split-Path -Parent $PSScriptRoot          # 本文件在 scripts/ 下，上一级是技能根
$py     = Join-Path $skill '.venv\Scripts\python.exe'
$script = Join-Path $PSScriptRoot 'watchdog_resume.py'
$name   = 'BilibiliTranscriptWatchdog'

if (-not (Test-Path $py))     { throw "找不到解释器: $py（先按 references/SETUP.md 建 venv）" }
if (-not (Test-Path $script)) { throw "找不到看门狗脚本: $script" }

$action = New-ScheduledTaskAction -Execute $py `
                                  -Argument ('"{0}"' -f $script) `
                                  -WorkingDirectory $skill

$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
                              -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes))
)

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
                                         -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
                                         -StartWhenAvailable `
                                         -AllowStartIfOnBatteries `
                                         -DontStopIfGoingOnBatteries

$desc = '批次看门狗：意外断电或进程被杀后，确认批次已死则回收僵尸条目并以独立进程续跑。只续跑已存在的批次，绝不自作主张开新批。'

Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers `
                       -Settings $settings -Description $desc -Force | Out-Null

Get-ScheduledTask -TaskName $name |
    Select-Object TaskName, State, @{n = 'TriggerCount'; e = { $_.Triggers.Count } } |
    Format-List
Get-ScheduledTaskInfo -TaskName $name |
    Select-Object LastRunTime, LastTaskResult, NextRunTime | Format-List

Write-Host "已注册。判定与动作记录在 state\logs\watchdog.log"
