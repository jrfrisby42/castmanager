# Registers the "Cast Manager" scheduled task: starts at system startup, runs whether or not a
# user is logged on (S4U logon as the current user, so the per-user Python install and its
# packages are found), and restarts every minute if it fails.
# Run from an elevated PowerShell:  powershell -ExecutionPolicy Bypass -File .\install_task.ps1

$ErrorActionPreference = "Stop"
$TaskName = "Cast Manager"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Main = Join-Path $AppDir "main.py"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this script from an elevated (Run as administrator) PowerShell." }

if (-not (Test-Path (Join-Path $AppDir "config.json"))) {
    throw "config.json not found in $AppDir. Copy config.example.json to config.json and fill in the urls first."
}

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    $pythonw = Join-Path (Split-Path $python) "pythonw.exe"
}
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found." }
Write-Host "Using $pythonw"

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$Main`"" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Keeps the office TVs casting their dashboards. Web page: http://127.0.0.1:8765" `
    -Force | Out-Null

Write-Host "Registered task '$TaskName' for $user. Start it now with:  Start-ScheduledTask -TaskName '$TaskName'"
