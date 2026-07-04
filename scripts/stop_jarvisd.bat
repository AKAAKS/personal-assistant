@echo off
rem Stops jarvisd however it was started: ends the scheduled task, then
rem kills whatever process still holds the port (schtasks /End alone can
rem orphan the daemon if the task was registered with a cmd wrapper).
set PORT=%JARVIS_PORT%
if "%PORT%"=="" set PORT=8765
schtasks /End /TN "jarvisd" >nul 2>&1
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force; Write-Output ('Stopped jarvisd (PID ' + $_.OwningProcess + ')') }"
echo Done.
