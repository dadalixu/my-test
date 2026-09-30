@echo off
chcp 65001 >nul
cd /d "%~dp0"
title job_agent

rem ---------------------------------------------------------------
rem  Double-click this file to start the job assistant.
rem  It always uses the project's own .venv interpreter, so there is
rem  no way to accidentally run it with the wrong Python.
rem ---------------------------------------------------------------

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found in this folder.
    echo.
    echo Create it first, then run this file again:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r job_agent\requirements.txt
    echo.
    pause
    exit /b 1
)

rem --- health check: are the packages actually installed in .venv? ---
".venv\Scripts\python.exe" -c "import langgraph, langchain_openai, langchain_core" 2>nul
if errorlevel 1 (
    echo [ERROR] .venv exists but the packages are NOT installed.
    echo.
    echo Fix it with this one command:
    echo     .venv\Scripts\python.exe -m pip install -r job_agent\requirements.txt
    echo.
    pause
    exit /b 1
)

echo Starting... ^(using .venv\Scripts\python.exe^)
echo.

".venv\Scripts\python.exe" -X utf8 -m job_agent

echo.
echo ---------------------------------------------------------------
echo  Window kept open on purpose.
echo  If you saw an error above, send me that text and I will fix it.
echo ---------------------------------------------------------------
pause >nul
