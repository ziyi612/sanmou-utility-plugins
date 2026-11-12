@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title Battle Report Archive
if not defined SANMOU_TEMP_DIR set "SANMOU_TEMP_DIR=%~dp0.runtime\temp"
set "TEMP=%SANMOU_TEMP_DIR%"
set "TMP=%SANMOU_TEMP_DIR%"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0start_battle_archive.ps1" (
  echo [ERROR] Program entry is missing. Restore the project files and try again.
  if not "%BATTLE_ARCHIVE_NO_PAUSE%"=="1" pause
  exit /b 2
)
echo Battle Report Archive - uses the current project files on every launch.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_battle_archive.ps1" %*
set "BATTLE_ARCHIVE_EXIT=%ERRORLEVEL%"
if "%BATTLE_ARCHIVE_EXIT%"=="0" exit /b 0
echo [ERROR] Startup or collection did not complete. See the message above.
if not "%BATTLE_ARCHIVE_NO_PAUSE%"=="1" pause
exit /b %BATTLE_ARCHIVE_EXIT%
