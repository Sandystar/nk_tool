@echo off
cd /d "%~dp0"
rem No arguments: full patch (recon -> plan -> confirm -> apply -> activate).
rem Useful flags: --recon  --pull-only  --verify  --restore  --yes
python python_tool\patch_docker.py %*
pause
