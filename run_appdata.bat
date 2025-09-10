@echo off
setlocal enabledelayedexpansion

rem ============================================================================
rem run_appdata.bat
rem - Copies the app into a temp workspace under %LOCALAPPDATA%
rem - Checks or installs Python (silent, via setup.vbs and official installer)
rem - Creates venv in the temp workspace, installs requirements
rem - Launches app.py using the venv Python
rem ============================================================================

rem Determine source application directory (folder where this .bat resides)
set "SRC_DIR=%~dp0"
pushd "%SRC_DIR%"

echo [INFO] Starting AppData run...

rem --------------------------------------------------------------------------
rem Resolve AppData base and workspace paths
rem --------------------------------------------------------------------------
set "BASE_DIR=%SRC_DIR:~0,-1%"
for %%I in ("%BASE_DIR%") do set "APP_BASENAME=%%~nI"
if not defined APP_BASENAME set "APP_BASENAME=RepoRunner"

set "USER_BASE=%LOCALAPPDATA%"
if not defined USER_BASE set "USER_BASE=%LocalAppData%"
if not defined USER_BASE set "USER_BASE=%TEMP%"

set "WORK_ROOT=%USER_BASE%\%APP_BASENAME%"
set "WORK_DIR=%WORK_ROOT%\temp"

echo [INFO] Workspace: "%WORK_DIR%"

rem Prepare workspace
if exist "%WORK_DIR%" rmdir /S /Q "%WORK_DIR%"
mkdir "%WORK_DIR%" >nul 2>&1

rem --------------------------------------------------------------------------
rem Copy source files into workspace (exclude venv/.git/__pycache__/app_error.log)
rem --------------------------------------------------------------------------
where robocopy >nul 2>&1
if %errorlevel%==0 (
    echo [INFO] Copying files with robocopy...
    robocopy "%SRC_DIR%" "%WORK_DIR%" /MIR /R:2 /W:2 /NFL /NDL /NJH /NJS /XD venv .git __pycache__ /XF app_error.log >nul
    set "RC=%ERRORLEVEL%"
    if %RC% GEQ 8 (
        echo [WARN] Robocopy reported issues (code %RC%). Attempting to continue.
    )
) else (
    echo [INFO] Copying files with xcopy...
    xcopy "%SRC_DIR%*" "%WORK_DIR%\" /E /I /H /Y >nul
    if exist "%WORK_DIR%\venv" rmdir /S /Q "%WORK_DIR%\venv"
    if exist "%WORK_DIR%\.git" rmdir /S /Q "%WORK_DIR%\.git"
    if exist "%WORK_DIR%\__pycache__" rmdir /S /Q "%WORK_DIR%\__pycache__"
    if exist "%WORK_DIR%\app_error.log" del /F /Q "%WORK_DIR%\app_error.log"
)

rem Ensure critical files exist
if not exist "%WORK_DIR%\app.py" (
    echo [ERROR] Workspace copy failed (missing app.py). Aborting.
    pause
    popd
    endlocal
    exit /b 1
)

rem --------------------------------------------------------------------------
rem Check or install Python
rem --------------------------------------------------------------------------
echo [INFO] Checking for Python on PATH...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Python not found on PATH. Will attempt silent installation...

    rem If python_installer.exe not present (in workspace), download it there
    if not exist "%WORK_DIR%\python_installer.exe" (
        echo [INFO] Downloading official Python installer...
        call :DownloadPythonInstaller "%WORK_DIR%"
        if not exist "%WORK_DIR%\python_installer.exe" (
            echo [ERROR] Could not obtain python_installer.exe automatically.
            echo        Please download from https://www.python.org/downloads/windows/
            echo        and place it as: %WORK_DIR%\python_installer.exe
            pause
            popd
            endlocal
            exit /b 1
        )
    )

    rem Ensure setup.vbs exists in workspace; if missing, try to copy from source
    if not exist "%WORK_DIR%\setup.vbs" (
        if exist "%SRC_DIR%setup.vbs" (
            copy /Y "%SRC_DIR%setup.vbs" "%WORK_DIR%\" >nul
        )
    )
    if not exist "%WORK_DIR%\setup.vbs" (
        echo [ERROR] setup.vbs not found in workspace. It is required for silent install.
        echo         Ensure setup.vbs is placed next to run_appdata.bat or app.py.
        pause
        popd
        endlocal
        exit /b 1
    )

    echo [INFO] Running silent installer...
    pushd "%WORK_DIR%"
    wscript //nologo "%WORK_DIR%\setup.vbs"
    popd
    echo [INFO] Python installer finished. Verifying...
)

rem Try to locate python.exe explicitly after potential install
set "PYTHON_EXE="
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "usebackq delims=" %%P in (`where python`) do (
        set "PYTHON_EXE=%%P"
        goto :HavePython
    )
)

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
    echo        Please ensure Python is installed and available on PATH, then rerun.
    pause
    popd
    endlocal
    exit /b 1
)
echo [INFO] Using Python: %PYTHON_EXE%

rem --------------------------------------------------------------------------
rem Create venv in workspace and install requirements
rem --------------------------------------------------------------------------
if not exist "%WORK_DIR%\venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment in workspace...
    "%PYTHON_EXE%" -m venv "%WORK_DIR%\venv"
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        popd
        endlocal
        exit /b 1
    )
) else (
    echo [INFO] Virtual environment already exists in workspace.
)

echo [INFO] Upgrading pip in workspace venv...
"%WORK_DIR%\venv\Scripts\python.exe" -m pip install --upgrade pip

if exist "%WORK_DIR%\requirements.txt" (
    echo [INFO] Installing requirements from requirements.txt (workspace)...
    "%WORK_DIR%\venv\Scripts\python.exe" -m pip install -r "%WORK_DIR%\requirements.txt"
) else (
    echo [WARN] requirements.txt not found in workspace. Skipping dependency install.
)

rem --------------------------------------------------------------------------
rem Launch the GUI application from workspace
rem --------------------------------------------------------------------------
echo [INFO] Launching application from workspace...
pushd "%WORK_DIR%"
"%WORK_DIR%\venv\Scripts\python.exe" "%WORK_DIR%\app.py"
set "EXITCODE=%ERRORLEVEL%"
popd

echo [INFO] Application exited with code %EXITCODE%
echo.
echo Press any key to close this window...
pause >nul
popd
endlocal
exit /b %EXITCODE%

goto :eof

:DownloadPythonInstaller
rem %1 -> destination directory to save python_installer.exe
setlocal
set "DST=%~1"
if not defined DST set "DST=%CD%"
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

powershell -NoProfile -ExecutionPolicy Bypass -Command "try {Invoke-WebRequest -UseBasicParsing -Uri '%PY_URL%' -OutFile '%DST%\python_installer.exe'; exit 0} catch {exit 1}"
if exist "%DST%\python_installer.exe" (
    echo [INFO] Downloaded with PowerShell.
    endlocal & exit /b 0
)

where curl >nul 2>&1
if %errorlevel%==0 (
    curl -L -o "%DST%\python_installer.exe" "%PY_URL%"
    if exist "%DST%\python_installer.exe" (
        echo [INFO] Downloaded with curl.
        endlocal & exit /b 0
    )
)

where bitsadmin >nul 2>&1
if %errorlevel%==0 (
    bitsadmin /transfer "python_download" /download /priority normal "%PY_URL%" "%DST%\python_installer.exe" >nul 2>&1
    if exist "%DST%\python_installer.exe" (
        echo [INFO] Downloaded with BITSAdmin.
        endlocal & exit /b 0
    )
)

echo [WARN] Automatic download failed.
endlocal & exit /b 1