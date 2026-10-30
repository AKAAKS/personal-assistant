@echo off
rem Installs the JARVIS tray launcher to start at logon (no terminal needed).
rem Creates a shortcut in your Startup folder that runs the tray app via pythonw
rem (windowless). Re-run after pulling changes to this script.
rem If you use a venv, edit PYTHONW below to point at <venv>\Scripts\pythonw.exe.

setlocal
set PROJ=%~dp0..
set PYTHONW=pythonw
for /f "delims=" %%I in ('where pythonw 2^>nul') do set "PYTHONW=%%I"

set TARGET=%PYTHONW%
set LAUNCHER=%PROJ%\scripts\jarvis_tray_launcher.py
set LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JARVIS.lnk

rem The tray app now owns the brain's lifecycle, so retire the old daemon-only
rem logon task to avoid two daemons racing for the port.
schtasks /Delete /TN "jarvisd" /F >nul 2>&1

powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%');" ^
  "$s.TargetPath='%TARGET%';" ^
  "$s.Arguments='\"%LAUNCHER%\"';" ^
  "$s.WorkingDirectory='%PROJ%';" ^
  "$s.WindowStyle=7;" ^
  "$s.Description='JARVIS voice assistant (tray)';" ^
  "$s.Save()"

if %errorlevel%==0 (
  echo JARVIS tray installed. It will start automatically at your next logon.
  echo Start it right now without logging out:
  echo     start "" "%PYTHONW%" "%LAUNCHER%"
  echo Remove it with: scripts\uninstall_tray.bat
) else (
  echo FAILED to create the Startup shortcut.
)
endlocal
