"""Launcher for the scheduled task: makes pythonw the task's root process
so `schtasks /End /TN jarvisd` actually kills the daemon (a cmd-wrapper
root would be killed instead, orphaning the daemon)."""

import os
import runpy
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(root)
sys.path.insert(0, root)
runpy.run_module("jarvisd", run_name="__main__")
