@echo off
REM ═══════════════════════════════════════════════════════════════════
REM  Egyszeri futas - Windows Feladatutemezohoz (orankenti kiertekeles).
REM
REM  A Feladatutemezoben a "Program/script" ez a fajl, az argumentumok
REM  pedig pl.:
REM      --trade --notify-once --testnet --margin-usdt 1 --leverage 20
REM
REM  Parameterek nelkul csak kiirja a jelzeseket (nem kereskedik) - ez a
REM  biztonsagos alap, ezert a kapcsolokat mindig a feladatban add meg.
REM
REM  A kimenet a logs\signals.log fajlba kerul (hozzafuzve).
REM ═══════════════════════════════════════════════════════════════════
setlocal
cd /d "%~dp0"

if not exist logs mkdir logs
set LOG=logs\signals.log

>> "%LOG%" echo.
>> "%LOG%" echo ===== %DATE% %TIME% ===== %*
python pivot_signals.py %* >> "%LOG%" 2>&1
set CODE=%ERRORLEVEL%
>> "%LOG%" echo ----- exit: %CODE%
endlocal & exit /b %CODE%
