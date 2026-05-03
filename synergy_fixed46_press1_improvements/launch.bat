@echo off
title Synergy
echo  Starting Synergy backend...
start "Synergy Backend" cmd /k "cd /d %~dp0backend && python synergy.py"

echo  Waiting for backend...
:wait
timeout /t 2 /nobreak >nul
powershell -Command "try{Invoke-WebRequest http://localhost:5050/api/state -UseBasicParsing -TimeoutSec 1|Out-Null;exit 0}catch{exit 1}"
if %errorLevel% NEQ 0 goto wait

echo  Backend ready — launching UI...
cd /d "%~dp0electron"
call npx electron . 2>nul
if %errorLevel% NEQ 0 call node_modules\.bin\electron . 2>nul
