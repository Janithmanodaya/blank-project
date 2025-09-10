@echo off
setlocal enabledelayedexpansion

rem =============================================================================
rem Build script for creating a single 64-bit Windows EXE using PyInstaller
rem - Creates a local build virtualenv (.buildenv)
rem - Installs PyInstaller and project requirements
rem - Bundles launcher.py (which boots app.py) into a onefile, windowed EXE
rem - Optionally includes pro.jpg if present
rem - Optionally sets icon if icon.ico is present
rem =============================================================================

set "ROOT=%~dp0"
pushd "%ROOT%"

set "VENV=.buildenv"
set "PY=%VENV%\Scripts\python.exe"
set "PIP=%VENV%\Scripts\pip.exe"
set "PYI=%VENV%\Scripts\pyinstaller.exe"

if not exist "%VENV%\Scripts\python.exe" (
    echo [INFO] Creating build virtualenv...
    py -3 -m venv "%VENV%"
    if errorlevel 1 (
        echo [ERROR] Failed to create build virtualenv. Ensure Python 3 is installed.
        pause
        exit /b 1
    )
)

echo [INFO] Upgrading pip...
"%PY%" -m pip install --upgrade pip

echo [INFO] Installing build dependencies (pyinstaller)...
"%PIP%" install pyinstaller

echo [INFO] Installing project dependencies...
if exist "%ROOT%requirements.txt" (
    "%PIP%" install -r "%ROOT%requirements.txt"
) else (
    rem Minimal set in case requirements.txt is missing
    "%PIP%" install customtkinter gitpython pillow
)

rem Prepare optional data includes
set "ADD_DATA="
if exist "%ROOT%pro.jpg" (
    set "ADD_DATA=--add-data ""pro.jpg;."""
)

rem Optional icon
set "ICON_ARG="
if exist "%ROOT%icon.ico" (
    set "ICON_ARG=--icon ""icon.ico"""
)

rem Clean previous dist/build
if exist "%ROOT%dist" rmdir /S /Q "%ROOT%dist"
if exist "%ROOT%build" rmdir /S /Q "%ROOT%build"
if exist "%ROOT%RepoRunner.spec" del /F /Q "%ROOT%RepoRunner.spec"

echo [INFO] Building onefile executable...
"%PYI%" --onefile --noconsole --name "RepoRunner" %ICON_ARG% %ADD_DATA% launcher.py
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo [ERROR] PyInstaller build failed with code %RC%.
    pause
    exit /b %RC%
)

echo [INFO] Build complete. Output: "%ROOT%dist\RepoRunner.exe"
pause

popd
endlocal