# Optional (Windows 11): keep the TeleFi hub running in the background as a scheduled task.
# pythonw.exe runs Python with no console window, so nothing pops up. One instance only; a
# 5-minute trigger restarts it if it stopped. TeleFi must be the bot's ONLY reader of replies:
# pass -DisableTask for any older task that polled the same bot (disabled only after TeleFi_Hub
# is registered).
#
#   powershell -NoProfile -File examples/windows-service.ps1 [-Pythonw <path to pythonw.exe>] [-DisableTask <OldPoller>]
param([string]$Pythonw = (Join-Path (Split-Path (Get-Command python).Source) "pythonw.exe"), [string[]]$DisableTask = @())
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $Pythonw)) { throw "pythonw.exe not found at $Pythonw; pass -Pythonw" }
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m telefi serve"
# Every 5 minutes; an at-logon trigger would need elevation.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName "TeleFi_Hub" -Action $action -Trigger $trigger -Settings $settings -Description "TeleFi hub: deliver the spool and handle Telegram replies" -Force -ErrorAction Stop | Out-Null
if (-not (Get-ScheduledTask -TaskName "TeleFi_Hub" -ErrorAction SilentlyContinue)) { throw "TeleFi_Hub not registered; nothing else changed" }
foreach ($t in $DisableTask) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { Disable-ScheduledTask -TaskName $t | Out-Null; Write-Output "disabled $t" }
}
Write-Output "registered TeleFi_Hub ($Pythonw -m telefi serve)"
