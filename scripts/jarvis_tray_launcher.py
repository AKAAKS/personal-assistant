"""Launcher for the JARVIS tray app (target of the Startup-folder shortcut).

Mirrors jarvisd_launcher.py: root the process at the repo so data/ and logs/
resolve, then run the tray module. Invoke via pythonw.exe for no console window.
"""

import os
import runpy
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(root)
sys.path.insert(0, root)
runpy.run_module("clients.tray_app", run_name="__main__")
