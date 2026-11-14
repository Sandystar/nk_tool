@echo off
cd /d "%~dp0"
python license_server\build.py %*
pause
