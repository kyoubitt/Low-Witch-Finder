# Low Witch Finder installer for Windows.
#
#   irm https://raw.githubusercontent.com/kyoubitt/Low-Witch-Finder/master/install.ps1 | iex
#
# Run it again any time to update. To remove it:
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/kyoubitt/Low-Witch-Finder/master/install.ps1))) -Uninstall
#
# No admin rights: it goes in %LOCALAPPDATA%\Low Witch Finder, with a Start menu shortcut and the folder added to your PATH,
# so you can open a terminal and type  LowWitchFinder  (your hunts and finds stay in that folder across updates).
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$repo = "kyoubitt/Low-Witch-Finder"
$dest = Join-Path $env:LOCALAPPDATA "Low Witch Finder"
$lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Low Witch Finder.lnk"

function Remove-FromPath($dir) {
    $p = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($p) { [Environment]::SetEnvironmentVariable("Path", (($p -split ";" | Where-Object { $_ -and $_ -ne $dir }) -join ";"), "User") }
}

if ($Uninstall) {
    Write-Host "removing Low Witch Finder" -ForegroundColor Cyan
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest; Write-Host "removed $dest (its hunts and finds went with it)" -ForegroundColor DarkGray }
    if (Test-Path $lnk) { Remove-Item -Force $lnk }
    Remove-FromPath $dest
    Write-Host "done" -ForegroundColor Green
    return
}

Write-Host "Low Witch Finder -> $dest" -ForegroundColor Cyan
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers @{ "User-Agent" = "low-witch-finder-installer" }
$asset = $rel.assets | Where-Object { $_.name -eq "LowWitchFinder-windows.zip" } | Select-Object -First 1
if (-not $asset) { throw "the latest release has no LowWitchFinder-windows.zip" }
Write-Host ("downloading " + $rel.tag_name + " (" + [math]::Round($asset.size / 1MB) + " MB)") -ForegroundColor DarkGray
$zip = Join-Path $env:TEMP "LowWitchFinder-windows.zip"
$tmp = Join-Path $env:TEMP "LowWitchFinder-unpack"
Invoke-WebRequest $asset.browser_download_url -OutFile $zip
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path $zip -DestinationPath $tmp -Force
New-Item -ItemType Directory -Force -Path $dest | Out-Null
# copy over the program, keeping what the user made (runs, results, Minecraft files)
Copy-Item -Path (Join-Path $tmp "LowWitchFinder\*") -Destination $dest -Recurse -Force
Remove-Item -Recurse -Force $tmp, $zip

$exe = Join-Path $dest "LowWitchFinder.exe"
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $exe
$s.WorkingDirectory = $dest
$s.Save()
$p = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not (($p -split ";") -contains $dest)) { [Environment]::SetEnvironmentVariable("Path", ($p.TrimEnd(";") + ";" + $dest), "User") }

Write-Host ""
Write-Host "Low Witch Finder is installed." -ForegroundColor Green
Write-Host "Open it from the Start menu, or in a new terminal type:  LowWitchFinder"
Write-Host "(For the deeper floors -55 to -59 you need an NVIDIA graphics card; the map and floors -50 to -54 work anywhere.)"
