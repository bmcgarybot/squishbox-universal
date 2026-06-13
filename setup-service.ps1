# SquishBox Background Service Setup (v2)
# Run this ONCE to set up auto-start at login + desktop shortcut

Write-Host ""
Write-Host "  Setting up SquishBox background service..." -ForegroundColor Cyan
Write-Host ""

# Find python path
$pythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonPath) {
    Write-Host "  [ERROR] Python not found in PATH" -ForegroundColor Red
    exit 1
}
Write-Host "  Using Python: $pythonPath" -ForegroundColor Gray

# 1. Remove old scheduled task if exists
Unregister-ScheduledTask -TaskName "SquishBox" -Confirm:$false -ErrorAction SilentlyContinue

# 2. Create a VBS launcher (truly hidden, no flash)
$vbsPath = "$HOME\squishbox\launch-hidden.vbs"
@"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$pythonPath"" ""$HOME\squishbox\app.py""", 0, False
"@ | Out-File -FilePath $vbsPath -Encoding ASCII
Write-Host "  [OK] Hidden launcher created" -ForegroundColor Green

# 3. Create scheduled task using the VBS launcher
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument """$vbsPath""" -WorkingDirectory "$HOME\squishbox"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval ([TimeSpan]::FromMinutes(1))
Register-ScheduledTask -TaskName "SquishBox" -Action $action -Trigger $trigger -Settings $settings -Description "SquishBox video transcoder" -Force | Out-Null
Write-Host "  [OK] Auto-start at login configured" -ForegroundColor Green

# 4. Kill any existing instance and start fresh
Stop-Process -Name python -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Start-Process python -ArgumentList "$HOME\squishbox\app.py" -WindowStyle Hidden -WorkingDirectory "$HOME\squishbox"
Write-Host "  [OK] SquishBox started in background" -ForegroundColor Green

# 5. Desktop shortcut (just opens browser)
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "SquishBox.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "http://localhost:5555"
$shortcut.Description = "Open SquishBox dashboard"
$shortcut.IconLocation = "shell32.dll,14"
$shortcut.Save()
Write-Host "  [OK] Desktop shortcut created" -ForegroundColor Green

Start-Sleep -Seconds 2
Start-Process "http://localhost:5555"

Write-Host ""
Write-Host "  All done! SquishBox is running." -ForegroundColor Green
Write-Host "  - Auto-starts when you log in (no window)" -ForegroundColor Cyan
Write-Host "  - Double-click desktop shortcut to open" -ForegroundColor Cyan
Write-Host "  - To stop: taskkill /im python.exe /f" -ForegroundColor Cyan
Write-Host ""
