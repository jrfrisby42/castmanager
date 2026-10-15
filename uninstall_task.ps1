# Stops and removes the "Cast Manager" scheduled task. Run from an elevated PowerShell.
$ErrorActionPreference = "Stop"
$TaskName = "Cast Manager"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) { Write-Host "Task '$TaskName' is not registered."; exit 0 }
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed task '$TaskName'."
