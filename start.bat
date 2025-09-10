@echo off
setlocal enabledelayedexpansion

rem Determine application directory (the folder where this .bat resides)
set "APP_DIR=%~dp0"
pushd "%APP_DIR%"

rem ============================================================================
rem Copy all files (except start.bat) to user's temp folder and work from there
rem Do not create a new subfolder; copy directly into %LocalAppData%\Temp
rem ============================================================================
set "TEMP_DST=%LocalAppData%\Temp"
if not defined LocalAppData set "TEMP_DST=%TEMP%"
if not exist "%TEMP_DST%" (
    echo [ERROR] Temp folder not found: %TEMP_DST%
    pause
    endlocal & exit /b 1
)

echo [INFO] Copying files to "%TEMP_DST%" (excluding start.bat, venv, .git, __pycache__)...
where robocopy >nul 2>&1
if %errorlevel%==0 (
    rem Use robocopy without mirroring to avoid deleting other temp files
    robocopy "%APP_DIR%" "%TEMP_DST%" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /XD venv .git __pycache__ /XF start.bat >nul
) else (
    xcopy "%APP_DIR%*" "%TEMP_DST%\" /E /I /H /Y >nul
    if exist "%TEMP_DST%\start.bat" del /F /Q "%TEMP_DST%\start.bat"
    if exist "%TEMP_DST%\.git" rmdir /S /Q "%TEMP_DST%\.git"
    if exist "%TEMP_DST%\venv" rmdir /S /Q "%TEMP_DST%\venv"
    if exist "%TEMP_DST%\__pycache__" rmdir /S /Q "%TEMP_DST%\__pycache__"
)

rem Switch working directory to temp
popd
pushd "%TEMP_DST%"
set "APP_DIR=%TEMP_DST%\"

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

rem Step 3: Create virtual environment (venv) inside the temp directory if it doesn't exist
if not exist "%APP_DIR%venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment...
    "%PYTHON_EXE%" -m venv "%APP_DIR%venv"
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [INFO] Virtual environment already exists.
)

rem Step 4: Install/upgrade pip and core dependencies
echo [INFO] Upgrading pip...
"%APP_DIR%venv\Scripts\python.exe" -m pip install --upgrade pip

if exist "%APP_DIR%requirements.txt" (
    echo [INFO] Installing requirements from requirements.txt...
    "%APP_DIR%venv\Scripts\python.exe" -m pip install -r "%APP_DIR%requirements.txt"
) else (
    echo [WARN] requirements.txt not found. Skipping dependency install.
)

rem Launch the GUI application
echo [INFO] Launching application from temp...
"%APP_DIR%venv\Scripts\python.exe" "%APP_DIR%app.py"
set EXITCODE=%ERRORLEVEL%

echo [INFO] Application exited with code %EXITCODE%
popd
endlocal
exit /b %EXITCODE%

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