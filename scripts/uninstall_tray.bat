@echo off
rem Removes the JARVIS tray Startup shortcut. Does not stop a running tray —
rem use the tray icon's Quit for that.
set LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JARVIS.lnk
if exist "%LNK%" (
  del /f /q "%LNK%"
  echo Removed the JARVIS tray Startup shortcut.
) else (
  echo No JARVIS tray Startup shortcut found.
)
