@echo off
setlocal
cd /d "%~dp0"
echo ========================================
echo   USB Control Agent - Build EXE
echo ========================================
echo.
echo [1/3] Menginstall dependencies...
py -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :error
py -m pip install pyinstaller
if errorlevel 1 goto :error

echo.
echo [2/3] Building EXE (butuh beberapa menit)...
py -m PyInstaller --clean --onefile --name USBControlAgent --hidden-import=win32timezone "%~dp0USBControlAgent.py"
if errorlevel 1 goto :error

echo.
echo [3/3] BUILD SELESAI!
echo ----------------------------------------
echo File EXE: %~dp0dist\USBControlAgent.exe
echo Ukuran  : 
for %%f in ("%~dp0dist\USBControlAgent.exe") do echo            %%~zf bytes
echo.
echo Cara pakai di komputer client:
echo   1. Copy USBControlAgent.exe ke PC target
echo   2. Klik kanan - Run as Administrator
echo   3. Pilih mode: --enroll
echo.
pause
exit /b 0

:error
echo.
echo [GAGAL] Build gagal. Pastikan:
echo   - Python 3.8+ sudah terinstall
echo   - pip sudah terinstall
echo   - Koneksi internet aktif
pause
exit /b 1
