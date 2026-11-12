[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DesktopDirectory = [Environment]::GetFolderPath('DesktopDirectory'),
    [switch]$AllowDesktopOnC
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$temporary = if ($env:SANMOU_TEMP_DIR) { $env:SANMOU_TEMP_DIR } else { Join-Path $root ".runtime\temp" }
New-Item -ItemType Directory -Path $temporary -Force | Out-Null
$env:TEMP = $temporary
$env:TMP = $temporary
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$target = Join-Path $root "start_battle_archive.cmd"
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Missing program launcher: $target" }
if (-not (Test-Path -LiteralPath $DesktopDirectory -PathType Container)) { throw "Desktop directory does not exist: $DesktopDirectory" }
$desktop = (Resolve-Path -LiteralPath $DesktopDirectory).Path
# A stable forwarder reads the current project launcher every time. Software
# updates do not require another copy of application code on the desktop.
$escapedTarget = $target.Replace('%', '%%')
$content = @"
@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem SANMOU_BATTLE_ARCHIVE_DESKTOP_FORWARDER_V1
if not exist "$escapedTarget" (
  echo [ERROR] Battle Report Archive was moved or removed. Reinstall the desktop launcher.
  if not "%BATTLE_ARCHIVE_NO_PAUSE%"=="1" pause
  exit /b 2
)
call "$escapedTarget" %*
exit /b %errorlevel%
"@
$launcherName = ([string][char]0x6218) + [char]0x62A5 + [char]0x91C7 + [char]0x96C6 + '.cmd'
$destination = Join-Path $desktop $launcherName
if (Test-Path -LiteralPath $destination) {
    $old = [IO.File]::ReadAllText($destination)
    if ($old -notmatch 'SANMOU_BATTLE_ARCHIVE_DESKTOP_FORWARDER_V1') {
        throw "A different file already uses this desktop filename; it was not overwritten: $destination"
    }
}
[IO.File]::WriteAllText($destination, $content.Replace("`r`n", "`n").Replace("`n", "`r`n"), [Text.UTF8Encoding]::new($false))
Write-Output $destination
