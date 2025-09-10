@echo off
rem Always keeps console open while running start.bat
pushd "%~dp0"
title Repo Runner Debug Console
echo [INFO] Launching start.bat in persistent console...
echo.
cmd /k call "%~dp0start.bat"
popd