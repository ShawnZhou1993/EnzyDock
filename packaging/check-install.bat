@echo off
REM ===========================================================================
REM  EnzyDock - is this copy complete?
REM
REM  Double-click this file after unpacking.  It answers one question: did every
REM  file survive the transfer?  A half-copied folder is by far the most common
REM  reason for "Failed to load Python DLL ... LoadLibrary: The specified
REM  module could not be found", and no download can fix that.
REM
REM  Deliberately ASCII-only and free of external tools (no find.exe, no
REM  PowerShell, no Python): it has to run on any Windows, in any code page,
REM  on a machine where nothing is installed.
REM ===========================================================================
setlocal enabledelayedexpansion
title EnzyDock - check this copy is complete
cd /d "%~dp0"

echo.
echo ============================================================
echo   EnzyDock - is this copy complete?
echo ============================================================
echo.
echo   Folder: %CD%
echo.

set PROBLEMS=0

echo --- files that must be present ---------------------------------
call :need "EnzyDock.exe"
call :need "_internal\python312.dll"
call :need "_internal\VCRUNTIME140.dll"
call :need "_internal\VCRUNTIME140_1.dll"
call :need "_internal\PySide6\Qt6WebEngineCore.dll"
call :need "_internal\PySide6\QtWebEngineProcess.exe"
call :need "_internal\app\resources\viewer\3Dmol.min.js"
call :need "_internal\app\resources\bin\vina.exe"
call :need "_internal\app\resources\licenses\AutoDock-Vina-LICENSE.txt"
call :need "_internal\meeko\data\residue_chem_templates.json"

echo.
echo --- file count -------------------------------------------------
REM  Pure batch on purpose: piping into find.exe breaks when another find.exe
REM  comes first on PATH, which happens more often than one would think.
set COUNT=0
for /r "_internal" %%f in (*) do set /a COUNT+=1
echo   files inside _internal : %COUNT%
echo   a complete copy has     535 or more
if %COUNT% LSS 535 (
    echo   [FAIL] files are missing - the copy never finished
    set /a PROBLEMS+=1
) else (
    echo   [ OK ] the file count looks right
)

echo.
echo --- result -----------------------------------------------------
if !PROBLEMS! EQU 0 (
    echo   This copy looks complete.  Double-click EnzyDock.exe to start.
) else (
    echo   !PROBLEMS! problem^(s^) found - do NOT run EnzyDock.exe yet.
    echo.
    echo   1. Ask for the ZIP again and extract it to a short path, e.g.
    echo          C:\EnzyDock
    echo      Never run it from the WeChat received-files folder.
    echo   2. Turn off real-time protection in your antivirus
    echo      ^(360 / PC Manager / Windows Defender^) before extracting,
    echo      or add C:\EnzyDock to its whitelist.  Antivirus programs
    echo      delete DLLs from _internal and produce exactly this error.
    echo   3. See HOW-TO-RUN.txt for the full checklist.
)
echo.
pause
exit /b 0

:need
if exist %1 (
    echo   [ OK ] %~1
) else (
    echo   [FAIL] %~1 is MISSING
    set /a PROBLEMS+=1
)
exit /b 0
