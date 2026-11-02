# Optional, explicit installer. Creates only the Phase 5 read-only daily report task.
# Run after Analytics consent: powershell -ExecutionPolicy Bypass -File scripts\schedule-analytics.ps1
param([string]$At = '19:00')
$projectRoot = Split-Path -Parent $PSScriptRoot
$analyticsPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$action = New-ScheduledTaskAction -Execute $analyticsPython -Argument 'main.py analytics daily --comments' -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName 'YT Automation - Growth Report' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Refreshes owner-channel metrics and local growth reports; no publishing, comments or messages' -Force | Out-Null
Write-Host "Growth Report scheduled at $At. Reports: data\outputs\analytics\index.html"
