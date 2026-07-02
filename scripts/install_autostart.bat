@echo off
rem Registers jarvisd to start hidden at logon (Task Scheduler).
rem If you use a venv, edit PYTHONW below to point at <venv>\Scripts\pythonw.exe
set PROJ=%~dp0..
set PYTHONW=pythonw
schtasks /Create /F /TN "jarvisd" /SC ONLOGON ^
  /TR "cmd /c cd /d \"%PROJ%\" && %PYTHONW% -m jarvisd"
if %errorlevel%==0 (
  echo jarvisd registered. It will start at next logon.
  echo Start it now with:  schtasks /Run /TN "jarvisd"
) else (
  echo FAILED - run this script from an elevated prompt if access was denied.
)
