# Creates a desktop shortcut for SquishBox
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "SquishBox.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "cmd.exe"
$shortcut.Arguments = "/c cd /d `"$HOME\squishbox`" && python app.py"
$shortcut.WorkingDirectory = "$HOME\squishbox"
$shortcut.Description = "Launch SquishBox video transcoder"
$shortcut.Save()
Write-Host ""
Write-Host "  SquishBox shortcut created on Desktop!" -ForegroundColor Green
Write-Host "  Double-click it to start, then open http://localhost:5555" -ForegroundColor Cyan
Write-Host ""
