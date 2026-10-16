# Ярлык «Парковка Акжол» на рабочем столе + автозапуск при включении ПК
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ws = New-Object -ComObject WScript.Shell
$icon = Join-Path $dir 'static\parking.ico'

$lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Парковка Акжол.lnk'))
$lnk.TargetPath = "$env:SystemRoot\System32\wscript.exe"
$lnk.Arguments = '"' + (Join-Path $dir 'Parking.vbs') + '"'
$lnk.WorkingDirectory = $dir
$lnk.IconLocation = $icon
$lnk.Save()

# при включении ПК — только сервер и экран оплаты (окно оператора открывается ярлыком)
$auto = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'Парковка Акжол.lnk'))
$auto.TargetPath = "$env:SystemRoot\System32\wscript.exe"
$auto.Arguments = '"' + (Join-Path $dir 'Parking.vbs') + '" autostart'
$auto.WorkingDirectory = $dir
$auto.IconLocation = $icon
$auto.Save()
Write-Host 'Ярлык и автозапуск созданы.'
