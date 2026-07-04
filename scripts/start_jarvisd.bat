@echo off
rem Starts the registered jarvisd task now (register with install_autostart.bat).
schtasks /Run /TN "jarvisd" >nul 2>&1
if not %errorlevel%==0 (
  echo Task not registered - run scripts\install_autostart.bat first.
  exit /b 1
)
echo jarvisd starting in the background (models take ~30s to load).
echo Watch it come up with:  powershell Get-Content logs\jarvisd.log -Wait -Tail 5
echo Ready when the log shows "jarvisd listening".
