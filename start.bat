@echo off
setlocal enabledelayedexpansion

rem Determine application directory (the folder where this .bat resides)
set "APP_DIR=%~dp0"
pushd "%APP_DIR%"

echo [INFO] Starting setup...

rem Step 1: Check if Python is available
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Python not found. Preparing to install Python silently...

    rem Ensure we have an installer; download if missing
    if not exist "%APP_DIR%python_installer.exe" (
        echo [INFO] python_installer.exe not found. Attempting to download...
        call :DownloadPythonInstaller
        if not exist "%APP_DIR%python_installer.exe" (
            echo [ERROR] Could not obtain python_installer.exe automatically.
            echo Please download the official Python installer (e.g. from https://www.python.org/downloads/windows/),
            echo save it as python_installer.exe next to this start.bat, then rerun.
            pause
            exit /b 1
        )
    )

    if not exist "%APP_DIR%setup.vbs" (
        echo [ERROR] setup.vbs not found in %APP_DIR%
        echo The helper script is required to run the installer silently.
        pause
        exit /b 1
    )

    rem Step 2: Silent installation using helper VBS
    wscript //nologo "%APP_DIR%setup.vbs"
    echo [INFO] Python installer finished. Verifying Python availability...
)

rem Try to locate python.exe explicitly if where didn't find it (or to be robust after install)
set "PYTHON_EXE="
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "usebackq delims=" %%P in (`where python`) do (
        set "PYTHON_EXE=%%P"
        goto :HavePython
    )
)

rem Common install locations to probe
for %%D in ("%LocalAppData%\Programs\Python" "%ProgramFiles%\Python311" "%ProgramFiles%\Python312" "%ProgramFiles%\Python313" "%ProgramFiles(x86)%\Python311" "%ProgramFiles(x86)%\Python312" "%ProgramFiles(x86)%\Python313") do (
    if exist "%%~fD\python.exe" (
        set "PYTHON_EXE=%%~fD\python.exe"
        goto :HavePython
    )
    for /d %%V in ("%%~fD\Python*") do (
        if exist "%%~fV\python.exe" (
            set "PYTHON_EXE=%%~fV\python.exe"
            goto :HavePython
        )
    )
)

:HavePython
if not defined PYTHON_EXE (
    echo [ERROR] Python could not be found after installation attempt.
    echo Please ensure Python is installed and available on PATH, then rerun.
    pause
    exit /b 1
)

echo [INFO] Using Python: %PYTHON_EXE%

rem ---------------------------------------------------------------------------
rem Delegate to AppData runner which will:
rem  - create a temp workspace under AppData
rem  - create venv in that workspace
rem  - install requirements
rem  - launch app.py using the workspace venv
rem ---------------------------------------------------------------------------
if exist "%APP_DIR%run_appdata.bat" (
    echo [INFO] Handing off to run_appdata.bat ...
    call "%APP_DIR%run_appdata.bat"
    set "EXITCODE=%ERRORLEVEL%"
    echo [INFO] run_appdata.bat completed with code %EXITCODE%
    popd
    endlocal
    exit /b %EXITCODE%
) else (
    echo [ERROR] run_appdata.bat not found in %APP_DIR%
    echo         Please ensure run_appdata.bat exists next to start.bat.
    pause
    popd
    endlocal
    exit /b 1
)

goto :eof

:DownloadPythonInstaller
rem Decide best-matching Windows installer (defaults to 64-bit if supported)
set "PY_VER=3.12.5"
set "PY_ARCH_FILE="
if /I "%PROCESSOR_ARCHITECTURE%"=="AMD64" (
    set "PY_ARCH_FILE=python-%PY_VER%-amd64.exe"
) else (
    set "PY_ARCH_FILE=python-%PY_VER%.exe"
)
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/%PY_ARCH_FILE%"

echo [INFO] Downloading Python %PY_VER% from:
echo        %PY_URL%

rem Try PowerShell Invoke-WebRequest
powershell -NoProfile -ExecutionPolicy Bypass -Command "try {Invoke-WebRequest -UseBasicParsing -Uri '%PY_URL%' -OutFile '%APP_DIR%python_installer.exe'; exit 0} catch {exit 1}"
if exist "%APP_DIR%python_installer.exe" (
    echo [INFO] Downloaded with PowerShell.
    goto :eof
)

rem Try curl (available on Windows 10+)
where curl >nul 2>&1
if %errorlevel%==0 (
    curl -L -o "%APP_DIR%python_installer.exe" "%PY_URL%"
    if exist "%APP_DIR%python_installer.exe" (
        echo [INFO] Downloaded with curl.
        goto :eof
    )
)

rem Try bitsadmin as last resort
where bitsadmin >nul 2>&1
if %errorlevel%==0 (
    bitsadmin /transfer "python_download" /download /priority normal "%PY_URL%" "%APP_DIR%python_installer.exe" >nul 2>&1
    if exist "%APP_DIR%python_installer.exe" (
        echo [INFO] Downloaded with BITSAdmin.
        goto :eof
    )
)

echo [WARN] Automatic download failed.
goto :eof