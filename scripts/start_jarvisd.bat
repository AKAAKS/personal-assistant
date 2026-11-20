@echo off
rem Starts JARVIS whichever way THIS machine is set up.
rem
rem Two install modes exist (see README "Daemon-only logon task"):
rem   tray  - install_tray.bat; the tray owns the brain's lifecycle
rem   task  - install_autostart.bat; a daemon-only logon task, you run a client
rem
rem install_tray.bat deliberately DELETES the "jarvisd" task so only one owner
rem holds the port, so this script must never tell a tray user to run
rem install_autostart.bat -- that would give two owners racing port 8765.

setlocal
set PROJ=%~dp0..
set PORT=%JARVIS_PORT%
if "%PORT%"=="" set PORT=8765
set LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JARVIS.lnk
set LAUNCHER=%PROJ%\scripts\jarvis_tray_launcher.py

rem Resolve pythonw HERE, at top level. Inside a parenthesised if-block,
rem %PYTHONW% expands when the block is parsed -- before the for loop sets
rem it -- so "start" would get an empty program name and silently do nothing.
set PYTHONW=pythonw
for /f "delims=" %%I in ('where pythonw 2^>nul') do set "PYTHONW=%%I"

rem 1. Already listening? Then there is nothing to do.
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } exit 1"
if %errorlevel%==0 (
  echo jarvisd is already listening on %PORT% -- nothing to start.
  echo To restart it: use "Restart brain" in the tray menu, or
  echo   scripts\stop_jarvisd.bat  and then this script again.
  exit /b 0
)

rem 2. Tray running but brain down? Its watchdog revives the brain on its own.
powershell -NoProfile -Command "$p=Get-CimInstance Win32_Process -Filter \"Name like 'pythonw%%'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*tray_app*' -or $_.CommandLine -like '*jarvis_tray_launcher*' }; if ($p) { exit 0 } exit 1"
if %errorlevel%==0 (
  echo The JARVIS tray is already running and owns the brain's lifecycle.
  echo Its watchdog respawns a dead brain within ~15s; to not wait, right-click
  echo the tray icon and choose "Restart brain".
  exit /b 0
)

rem 3. Tray installed but not running: launch it. It starts the brain, then a client.
if exist "%LNK%" (
  echo Starting the JARVIS tray ^(it brings up the brain, then the voice client^)...
  start "" "%PYTHONW%" "%LAUNCHER%"
  echo Models take ~30s to load. Watch it come up with:
  echo   powershell Get-Content logs\jarvisd.log -Wait -Tail 5
  echo Ready when the log shows "jarvisd listening".
  exit /b 0
)

rem 4. No tray: fall back to the documented daemon-only logon task.
schtasks /Run /TN "jarvisd" >nul 2>&1
if %errorlevel%==0 (
  echo jarvisd starting in the background from the logon task ^(models ~30s^).
  echo   powershell Get-Content logs\jarvisd.log -Wait -Tail 5
  echo Ready when the log shows "jarvisd listening".
  exit /b 0
)

rem 5. Neither mode is installed. Explain both rather than guessing.
echo Nothing to start: no tray shortcut and no "jarvisd" logon task.
echo.
echo   Recommended -- tray owns the brain and can restart it:
echo       scripts\install_tray.bat
echo.
echo   Daemon only, you launch your own client:
echo       scripts\install_autostart.bat
echo.
echo   Just this once, no install, keeps a console open:
echo       python -m jarvisd
exit /b 1
