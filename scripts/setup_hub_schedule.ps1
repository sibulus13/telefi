# Registers TeleFi_Hub (Windows 11): `python -m telefi serve` delivers the spool and long-polls
# Telegram replies, hidden (no window). One instance only; a 5-minute trigger restarts it if it died.
# TeleFi must be the bot's ONLY getUpdates consumer: pass -DisableTask for any older task that polled
# the same bot; it is disabled only after TeleFi_Hub is registered.
#   powershell -NoProfile -File scripts/setup_hub_schedule.ps1 [-Python <path to python.exe>] [-DisableTask <OldPoller>]
param([string]$Python = "C:\Python312\python.exe", [string[]]$DisableTask = @())
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$vbs = Join-Path $PSScriptRoot "run-hidden.vbs"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B //Nologo `"$vbs`" `"$Python`" -m telefi serve" -WorkingDirectory $root
# Every 5 minutes; an at-logon trigger would need elevation.
$triggers = @(New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5))
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName "TeleFi_Hub" -Action $action -Trigger $triggers -Settings $settings -Description "TeleFi hub: deliver the spool and handle Telegram replies" -Force -ErrorAction Stop | Out-Null
if (-not (Get-ScheduledTask -TaskName "TeleFi_Hub" -ErrorAction SilentlyContinue)) { throw "TeleFi_Hub not registered; nothing else changed" }
foreach ($t in $DisableTask) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { Disable-ScheduledTask -TaskName $t | Out-Null; Write-Output "disabled $t" }
}
Write-Output "registered TeleFi_Hub"
