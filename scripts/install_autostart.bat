@echo off
rem Registers jarvisd to start hidden at logon (Task Scheduler).
rem If you use a venv, edit PYTHONW below to point at <venv>\Scripts\pythonw.exe
rem Re-run this script after pulling changes to it (it replaces the task).
set PROJ=%~dp0..
set PYTHONW=pythonw
schtasks /Create /F /TN "jarvisd" /SC ONLOGON ^
  /TR "\"%PYTHONW%\" \"%PROJ%\scripts\jarvisd_launcher.py\""
if %errorlevel%==0 (
  echo jarvisd registered. It will start at next logon.
  echo Start it now with:  schtasks /Run /TN "jarvisd"
  echo Stop it with:       scripts\stop_jarvisd.bat
) else (
  echo FAILED - run this script from an elevated prompt if access was denied.
)
