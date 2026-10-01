# Регистрирует задачу планировщика Windows: ежедневная синхронизация и переобучение.
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_sync.ps1 [-At 06:00] [-Remove]
# Задача запускает sync (с добором 100 свежих матчей) и train; лог — data\sync.log.

param(
    [string]$At = "06:00",
    [switch]$Remove
)

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root ".venv\Scripts\python.exe"
$name = "dota2bets sync"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "задача удалена"
    exit 0
}

$cmd = "cd /d `"$root`" && `"$python`" -m dota2bets sync --fresh 100 >> data\sync.log 2>&1 && `"$python`" -m dota2bets train >> data\sync.log 2>&1 & `"$python`" -m dota2bets book sync >> data\sync.log 2>&1 & `"$python`" -m dota2bets forecast --notify >> data\sync.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "задача '$name' зарегистрирована на $At ежедневно; лог data\sync.log"
