@echo off
rem Opens the YouTube Live channel editor in your browser (tools\editor.py). Close this window to stop it.
cd /d "%~dp0"
python tools\editor.py
if errorlevel 1 pause
