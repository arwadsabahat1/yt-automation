# Registers the Windows scheduled tasks that run the pipeline. Safe to run again: it updates them in place.
# Run once from PowerShell:  powershell -ExecutionPolicy Bypass -File scripts\schedule.ps1
#   YT Automation - Trend Radar       9:00 and 18:00  collect trending videos and score ideas
#   YT Automation - Daily Production  10:00           plan (Mondays), write, render, cut Shorts
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

$radar = New-ScheduledTaskAction -Execute $python -Argument "main.py run" -WorkingDirectory $root
Register-ScheduledTask -TaskName "YT Automation - Trend Radar" -Action $radar `
    -Trigger @((New-ScheduledTaskTrigger -Daily -At 9:00am), (New-ScheduledTaskTrigger -Daily -At 6:00pm)) `
    -Settings $settings -Description "Collects trending videos and scores new ideas" -Force | Out-Null

$daily = New-ScheduledTaskAction -Execute $python -Argument "main.py daily" -WorkingDirectory $root
Register-ScheduledTask -TaskName "YT Automation - Daily Production" -Action $daily `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 10:00am) `
    -Settings $settings -Description "Plans next week on Mondays, writes 3 scripts, renders videos and cuts Shorts" -Force | Out-Null

Write-Host "Scheduled: Trend Radar at 9:00 and 18:00, Daily Production at 10:00."
