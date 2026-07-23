@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Turnstile Solver
echo ============================================================

python solver_manager.py start
if errorlevel 1 goto FAIL

echo.
echo [OK] Solver start done.
echo Manage:
echo   python solver_manager.py status
echo   python solver_manager.py stop
echo.
pause
exit /b 0

:FAIL
echo.
echo [ERR] Start failed.
echo If deps missing, run:
echo   python setup_solver.py
echo.
echo If proxy error for GitHub/camoufox:
echo   1. Start your proxy on 7897, OR
echo   2. Temporarily clear proxy and retry:
echo      set HTTP_PROXY=
echo      set HTTPS_PROXY=
echo      set ALL_PROXY=
echo      python setup_solver.py
echo.
pause
exit /b 1
