@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Cleaning previous PyInstaller output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

if not exist main.spec (
    echo ERROR: main.spec not found.
    exit /b 1
)

echo [2/3] Building clean EXE...
python -m PyInstaller --clean main.spec
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    exit /b 1
)

echo [3/3] Preparing release folder...
if exist release rmdir /s /q release
mkdir release
copy /y "dist\Intelligent-Server-Operations-Platform.exe" "release\Intelligent-Server-Operations-Platform.exe" >nul

echo.
echo Build complete.
echo Release contains one EXE: %CD%\release\Intelligent-Server-Operations-Platform.exe
endlocal
