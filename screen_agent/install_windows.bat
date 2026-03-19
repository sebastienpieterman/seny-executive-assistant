@echo off
REM Seny Screen Agent — Windows Installation Script
REM Run this from the repo root directory by double-clicking or running in Command Prompt

echo.
echo === Seny Screen Agent — Windows Setup ===
echo.

REM Check Python is installed
python --version > nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Python is not installed.
    echo.
    echo Download it from: https://www.python.org/downloads
    echo.
    echo IMPORTANT: During installation, check the box that says "Add Python to PATH"
    echo Then run this script again.
    pause
    exit /b 1
)

REM Check we're in the right directory
if not exist "screen_agent\agent.py" (
    echo ERROR: Run this script from the repo root directory.
    echo Example: Open Command Prompt, navigate to the repo folder, then run:
    echo   screen_agent\install_windows.bat
    pause
    exit /b 1
)

REM Check .env exists, create from template if not
if not exist "screen_agent\.env" (
    copy "screen_agent\.env.example" "screen_agent\.env" > nul
    echo Created screen_agent\.env from template.
    echo.
    echo IMPORTANT: Edit screen_agent\.env and set your SCREEN_AGENT_KEY before continuing.
    echo Get your key from: Seny Settings - General - Screen Agent
    echo.
    echo Once you've added your key, run this script again.
    pause
    exit /b 0
)

REM Check SCREEN_AGENT_KEY is set
findstr /C:"your_key_here" "screen_agent\.env" > nul
if %ERRORLEVEL% == 0 (
    echo ERROR: You haven't set your SCREEN_AGENT_KEY in screen_agent\.env
    echo Get your key from: Seny Settings - General - Screen Agent
    pause
    exit /b 1
)

REM Install Python dependencies
echo Installing Python dependencies...
python -m pip install -r screen_agent\requirements-windows.txt --quiet
if %ERRORLEVEL% neq 0 (
    echo ERROR: pip install failed. Make sure Python is installed and in your PATH.
    pause
    exit /b 1
)
echo Dependencies installed.
echo.

REM Register Task Scheduler job (runs at login, uses pythonw to avoid terminal window)
echo Setting up auto-start...
schtasks /Create /TN "Seny Screen Agent" /SC ONLOGON /TR "\"%cd%\screen_agent\run_agent.bat\"" /RL LIMITED /F > nul
if %ERRORLEVEL% neq 0 (
    echo ERROR: Failed to create scheduled task.
    echo Try running this script as Administrator.
    pause
    exit /b 1
)

REM Create the runner batch file (used by Task Scheduler — runs pythonw silently)
(
echo @echo off
echo cd /d "%cd%"
echo pythonw screen_agent\agent.py
) > screen_agent\run_agent.bat

REM Start the agent now (don't wait for next login)
echo Starting agent...
start "" /B pythonw screen_agent\agent.py

echo.
echo === Done! ===
echo.
echo The Seny Screen Agent is now running and will auto-start on every login.
echo Look for the Seny icon in the system tray (bottom-right of your taskbar).
echo.
echo To pause the agent: right-click the tray icon - Pause
echo To stop the agent: right-click the tray icon - Quit
echo To uninstall: schtasks /Delete /TN "Seny Screen Agent" /F
echo.
pause
