$ws = New-Object -ComObject WScript.Shell
$startup = [Environment]::GetFolderPath('Startup')
$sc = $ws.CreateShortcut($startup + '\A-QuantTrading.lnk')
$sc.TargetPath = 'E:\wxf\claude\quant-trading\scripts\start_system.bat'
$sc.WorkingDirectory = 'E:\wxf\claude\quant-trading'
$sc.Description = 'A-Share Quant Trading System'
$sc.Save()
Write-Host 'Shortcut created:' $sc.FullName
