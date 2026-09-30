@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build Intelligent Server Operations Platform

set "APPNAME=Intelligent-Server-Operations-Platform"
set "BUILD_ENV=%~dp0.build-venv"
set "BUILD_PYTHON=%BUILD_ENV%\Scripts\python.exe"
set "SELFTEST_LOG=%TEMP%\ISOP-self-test-%RANDOM%-%RANDOM%.log"
set "ISOP_SELF_TEST_LOG=%SELFTEST_LOG%"

for %%F in (main.py main.spec requirements.txt runtime_tk.py paths.py logo.png arch_dark_theme.json theme_gray.json docker-compose.yml) do (
    if not exist "%%F" (
        echo [ERROR] Required project file is missing: %%F
        goto :failed
    )
)
if not exist "templates\dashboard.html" (
    echo [ERROR] Required web page is missing: templates\dashboard.html
    goto :failed
)
if exist "app\package.json" (
    echo Docker word-editor payload found; it will be included.
) else (
    echo [WARNING] app\package.json was not found. The EXE can still be built,
    echo           but the Docker word-editor feature will not be bundled.
)

if not exist "%BUILD_PYTHON%" goto :create_venv
"%BUILD_PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    echo [WARNING] Existing build environment is stale. Recreating it...
    goto :create_venv
)
"%BUILD_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [WARNING] Existing build environment uses an unsupported Python version. Recreating it...
    goto :create_venv
)
goto :venv_ready

:create_venv
echo Creating or repairing the isolated build environment...
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv --clear "%BUILD_ENV%"
        if not errorlevel 1 goto :venv_ready
        echo [WARNING] Python Launcher could not create the environment; trying python from PATH.
    )
)
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3 was not found in PATH or via the Python Launcher.
    echo Install Python 3.10 or newer, including Tcl/Tk, and try again.
    goto :failed
)
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The Python in PATH is older than Python 3.10.
    goto :failed
)
python -m venv --clear "%BUILD_ENV%"
if errorlevel 1 goto :venv_failed

:venv_ready

"%BUILD_PYTHON%" --version
if errorlevel 1 goto :python_failed
"%BUILD_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required to build this application.
    goto :failed
)
"%BUILD_PYTHON%" -c "import tkinter; t=tkinter.Tcl(); print('Tcl/Tk:', t.eval('info library'))"
if errorlevel 1 (
    echo [ERROR] This Python installation does not include a working Tcl/Tk runtime.
    echo Install or repair Python with Tcl/Tk support, then run this script again.
    goto :failed
)

echo.
echo Checking application and packaging dependencies...
"%BUILD_PYTHON%" -c "import importlib.util,sys; names=['customtkinter','psutil','pandas','yaml','flask','PIL','reportlab','werkzeug','PyInstaller']; missing=[n for n in names if importlib.util.find_spec(n) is None]; print('Missing packages: '+', '.join(missing) if missing else 'All required packages are available.'); sys.exit(bool(missing))"
if errorlevel 1 goto :install_deps
goto :build

:install_deps
echo.
echo Installing missing packages. Internet access to PyPI is needed only for this step.
"%BUILD_PYTHON%" -m pip install --disable-pip-version-check --prefer-binary --retries 1 --timeout 20 -r requirements.txt pyinstaller
if errorlevel 1 (
    echo.
    echo [ERROR] Required packages could not be installed.
    echo Check that this computer can access https://pypi.org, then run build_onefile.bat again.
    echo The build environment is at: %BUILD_ENV%
    goto :failed
)
"%BUILD_PYTHON%" -c "import importlib.util,sys; names=['customtkinter','psutil','pandas','yaml','flask','PIL','reportlab','werkzeug','PyInstaller']; missing=[n for n in names if importlib.util.find_spec(n) is None]; print('Missing packages after installation: '+', '.join(missing) if missing else 'Dependency check passed.'); sys.exit(bool(missing))"
if errorlevel 1 goto :deps_failed

:build
echo.
echo Building the one-file Windows application...
"%BUILD_PYTHON%" -m PyInstaller --clean --noconfirm main.spec
if errorlevel 1 goto :build_failed

if not exist "dist\%APPNAME%.exe" (
    echo [ERROR] PyInstaller finished without creating dist\%APPNAME%.exe
    goto :failed
)

echo.
echo ========================================
echo RUNNING FROZEN EXE SELF-TEST
echo ========================================
"dist\%APPNAME%.exe" --self-test-relaunch
set "TEST_RC=%ERRORLEVEL%"

echo.
if not exist "%SELFTEST_LOG%" (
    echo [ERROR] Self-test did not produce a report. The EXE may have crashed before startup.
    goto :selftest_failed
)
type "%SELFTEST_LOG%"
findstr /x /c:"=== SELF-TEST PASS ===" "%SELFTEST_LOG%" >nul
if errorlevel 1 (
    echo [ERROR] Self-test report does not contain a PASS result.
    goto :selftest_failed
)
if not "%TEST_RC%"=="0" goto :selftest_failed

echo.
echo ========================================
echo BUILD AND RUNTIME SELF-TEST SUCCEEDED
echo EXE: %CD%\dist\%APPNAME%.exe
echo ========================================
echo.
echo Distribution contains one file:
echo     %APPNAME%.exe
echo User data is stored in:
echo     %%LOCALAPPDATA%%\AI Server Management
echo.
pause
exit /b 0

:venv_failed
echo [ERROR] Could not create the isolated build environment.
goto :failed
:python_failed
echo [ERROR] The build Python environment could not start.
goto :failed
:deps_failed
echo [ERROR] One or more required packages are still unavailable.
goto :failed
:build_failed
echo [ERROR] PyInstaller build failed. Check the output above.
goto :failed
:selftest_failed
echo.
echo [ERROR] The frozen EXE self-test failed. See the report above and:
echo %SELFTEST_LOG%
goto :failed
:failed
echo.
echo Build did not complete. Existing build and dist files were left in place.
pause
exit /b 1
