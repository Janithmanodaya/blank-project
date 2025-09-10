@echo off
setlocal enabledelayedexpansion

rem Determine application directory (the folder where this .bat resides)
set "APP_DIR=%~dp0"
pushd "%APP_DIR%"

echo [INFO] Starting setup...

rem Step 1: Check if Python is available
where python &gt;nul 2&gt;&amp;1
if %errorlevel% neq 0 (
    echo [INFO] Python not found. Installing Python silently...
    rem Step 2: Silent installation using helper VBS
    if not exist "%APP_DIR%python_installer.exe" (
        echo [ERROR] python_installer.exe not found in %APP_DIR%
        echo Please download the official Python installer (e.g. from https://www.python.org/downloads/windows/)
        echo Save it as python_installer.exe next to this start.bat, then rerun.
        pause
        exit /b 1
    )
    if not exist "%APP_DIR%setup.vbs" (
        echo [ERROR] setup.vbs not found in %APP_DIR%
        echo The helper script is required to run the installer silently.
        pause
        exit /b 1
    )
    rem Use wscript to run VBS completely silent and wait
    wscript //nologo "%APP_DIR%setup.vbs"
    echo [INFO] Python installer finished. Verifying Python availability...
)

rem Try to locate python.exe explicitly if where didn't find it (or to be robust after install)
set "PYTHON_EXE="
where python &gt;nul 2&gt;&amp;1
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

rem Step 3: Create virtual environment (venv) inside the app directory if it doesn't exist
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
echo [INFO] Launching application...
"%APP_DIR%venv\Scripts\python.exe" "%APP_DIR%app.py"
set EXITCODE=%ERRORLEVEL%

echo [INFO] Application exited with code %EXITCODE%
popd
endlocal
exit /b %EXITCODE%