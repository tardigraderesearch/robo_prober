@echo off
rem robo-prober launcher for Windows. Double-click it, or run:
rem   run.bat                            open the range-of-motion window
rem   run.bat run-demo --out demo.json   any robo-prober command works too
rem
rem First run: finds Python 3.11+, makes a private .venv next to this file and
rem installs robo-prober into it. Later runs skip straight to launching unless
rem pyproject.toml, the Python version or the extras changed. Nothing is
rem installed system-wide.
rem   set ROBO_PROBER_PYTHON=C:\path\to\python.exe   pick a Python
rem   set ROBO_PROBER_EXTRAS=all                      also install camera / scan-to-URDF extras
rem                                                   (default: identify; none = core only)

setlocal EnableExtensions EnableDelayedExpansion
set "HERE=%~dp0"
set "VENV=%HERE%.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.robo-prober-installed"
set "VERSION_OK=import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
set "IMPORTS=import robo_prober, serial, PIL"
if not defined ROBO_PROBER_EXTRAS set "ROBO_PROBER_EXTRAS=identify"

rem 1. Find a Python 3.11+ ---------------------------------------------------
set "PY="
if defined ROBO_PROBER_PYTHON (
    "%ROBO_PROBER_PYTHON%" -c "%VERSION_OK%" >nul 2>&1 && set PY="%ROBO_PROBER_PYTHON%"
)
for %%C in ("py -3.13" "py -3.12" "py -3.11" "py -3" "python" "python3") do (
    if not defined PY (
        %%~C -c "%VERSION_OK%" >nul 2>&1 && set "PY=%%~C"
    )
)
if not defined PY (
    echo.
    echo [robo-prober] PROBLEM: Python 3.11 or newer was not found.
    echo     Install it from https://www.python.org/downloads/windows/
    echo     In the installer, tick "Add python.exe to PATH" and keep "tcl/tk and IDLE" ticked.
    echo     Then double-click run.bat again.
    goto :fail
)
for /f "delims=" %%V in ('"%PY% -c "import sys; print^('Python', sys.version.split^(^)[0]^)""') do set "PYVER=%%V"
echo [robo-prober] Using %PYVER% (%PY%)

rem 2. Tk is needed for the window only ---------------------------------------
set "NEEDS_WINDOW=0"
if "%~1"=="" set "NEEDS_WINDOW=1"
if /i "%~1"=="rom-gui" set "NEEDS_WINDOW=1"
%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    if "!NEEDS_WINDOW!"=="1" (
        echo.
        echo [robo-prober] PROBLEM: This Python has no tkinter, which the range-of-motion window needs.
        echo     Re-run the Python installer from https://www.python.org/downloads/windows/
        echo     choose "Modify" and tick "tcl/tk and IDLE". Then double-click run.bat again.
        echo     Without Tk you can still script it: run.bat rom --help
        goto :fail
    )
)

rem 3. Private virtual environment --------------------------------------------
if exist "%VPY%" (
    "%VPY%" -c "import sys" >nul 2>&1 || rmdir /s /q "%VENV%"
)
if not exist "%VPY%" (
    echo [robo-prober] First run: creating a private Python environment in .venv ^(one time only^)...
    %PY% -m venv "%VENV%"
    if errorlevel 1 (
        if exist "%VENV%" rmdir /s /q "%VENV%"
        echo.
        echo [robo-prober] PROBLEM: Could not create the virtual environment.
        echo     Reinstall Python from https://www.python.org/downloads/windows/ and try again.
        goto :fail
    )
)

rem 4. Install / update only when pyproject.toml, Python or extras changed --
set "WANT="
for /f "delims=" %%H in ('""%VPY%" -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], 'rb').read() + sys.version.encode() + sys.argv[2].encode()).hexdigest())" "%HERE%pyproject.toml" "%ROBO_PROBER_EXTRAS%""') do set "WANT=%%H"
if not defined WANT (
    echo [robo-prober] PROBLEM: Could not read pyproject.toml next to run.bat.
    goto :fail
)
set "HAVE="
if exist "%STAMP%" set /p HAVE=<"%STAMP%"
set "INSTALL=0"
if not "!WANT!"=="!HAVE!" set "INSTALL=1"
"%VPY%" -c "%IMPORTS%" >nul 2>&1 || set "INSTALL=1"
if "!INSTALL!"=="1" (
    echo [robo-prober] Installing robo-prober ^(extras: %ROBO_PROBER_EXTRAS%^). This needs internet once.
    "%VPY%" -m pip install --disable-pip-version-check --quiet --upgrade pip
    if /i "%ROBO_PROBER_EXTRAS%"=="none" (
        "%VPY%" -m pip install --disable-pip-version-check --quiet --upgrade --editable "%HERE%."
    ) else (
        "%VPY%" -m pip install --disable-pip-version-check --quiet --upgrade --editable "%HERE%.[%ROBO_PROBER_EXTRAS%]"
    )
    if errorlevel 1 (
        echo.
        echo [robo-prober] PROBLEM: Installing the Python libraries failed ^(see the pip messages above^).
        echo     Check your internet connection and run run.bat again.
        echo     To start over completely, delete the .venv folder.
        goto :fail
    )
    "%VPY%" -c "%IMPORTS%"
    if errorlevel 1 (
        echo.
        echo [robo-prober] PROBLEM: robo-prober installed but does not import ^(see the error above^).
        echo     Delete the .venv folder and run run.bat again.
        goto :fail
    )
    > "%STAMP%" echo !WANT!
    echo [robo-prober] Install complete.
) else (
    echo [robo-prober] Everything is already installed.
)

rem 5. Launch ------------------------------------------------------------------
if "%~1"=="" (
    echo [robo-prober] Opening the range-of-motion window...
    "%VPY%" -m robo_prober rom-gui
) else (
    "%VPY%" -m robo_prober %*
)
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo [robo-prober] robo-prober exited with code %CODE%.
    if "%~1"=="" pause
)
exit /b %CODE%

:fail
echo.
pause
exit /b 1
