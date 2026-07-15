@echo off
cd /d "%~dp0"
start "NEXUS Hardware Monitor" pythonw -m hardware_monitor.gui