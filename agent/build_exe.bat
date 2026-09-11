@echo off
setlocal
cd /d "%~dp0"
echo ========================================
echo   USB Control Agent - Build EXE
echo ========================================
echo.
echo [1/2] Menginstall dependencies...
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :error

echo.
echo [2/2] Building EXE (butuh beberapa menit)...
python -m PyInstaller --clean --onefile --name USBControlAgent --hidden-import=win32timezone "%~dp0USBControlAgent.py"
if errorlevel 1 goto :error

echo.
echo BUILD SELESAI!
echo File EXE: %~dp0dist\USBControlAgent.exe
echo.
echo Cara pakai di komputer client:
echo   1. Copy USBControlAgent.exe ke PC target
echo   2. Klik kanan - Run as Administrator
echo   3. Jalankan: USBControlAgent.exe --enroll
echo.
pause
exit /b 0

:error
echo.
echo [GAGAL] Build gagal. Pastikan:
echo   - Python 3.8+ sudah terinstall dan terdaftar di PATH
echo   - pip sudah terinstall (python -m pip)
echo   - Koneksi internet aktif
pause
exit /b 1
