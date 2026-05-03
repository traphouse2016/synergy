@echo off
echo Stopping GV Bot...
taskkill /f /im python.exe /fi "WINDOWTITLE eq GV Bot Backend" >nul 2>&1
taskkill /f /im electron.exe >nul 2>&1
taskkill /f /im Electron.exe >nul 2>&1
echo Done.
timeout /t 2 /nobreak >nul
